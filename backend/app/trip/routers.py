"""HTTP 路由层。只负责协议、参数与错误码，业务逻辑全部下沉到 service。

GET  /api/trip/cities                     热门城市宫格
GET  /api/trip/attractions?city=成都       景点池（heat 降序）
POST /api/trip/preview                    紧凑度预估（纯硬编码，毫秒级，不调 LLM）
POST /api/trip/plan                       生成行程（核心，LLM 并行流水线，10~30s）
POST /api/trip/plan/{id}/adjust           按倾向微调已有方案（纯硬编码，毫秒级）
GET  /api/trip/plan/{plan_id}             读取历史行程
GET  /api/trip/plans                      历史列表（「我的」页可选走后端）

注意路由顺序：`/plan/preview` 必须注册在 `/plan/{plan_id}` 之前，
否则 FastAPI 会把 "preview" 当成 plan_id 去匹配（int 转换失败 → 422）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.trip import llm, service
from app.trip.schemas import (
    AttractionOut,
    AutoPlanRequest,
    CityOut,
    PlanBrief,
    PlanPreview,
    PlanRequest,
    PlanResponse,
    SpotOut,
)

logger = logging.getLogger("triptide.routers")

router = APIRouter()

MAX_ATTRACTIONS = 40


def _guard(payload: PlanRequest) -> None:
    if len(payload.attraction_ids) > MAX_ATTRACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多勾选 {MAX_ATTRACTIONS} 个景点，请分批规划",
        )


@router.get("/cities", response_model=list[CityOut], summary="热门城市列表")
def get_cities(db: Session = Depends(get_db)) -> list[CityOut]:
    return service.list_cities(db)


@router.get("/attractions", response_model=list[AttractionOut], summary="城市景点池")
def get_attractions(
    city: str = Query(..., description="城市名或拼音，如 成都 / chengdu"),
    db: Session = Depends(get_db),
) -> list[AttractionOut]:
    return service.list_attractions(db, city)


@router.get(
    "/attractions/{attraction_id}/spots",
    response_model=list[SpotOut],
    summary="某景点的内部子景点（坐标 + 攻略）",
)
def get_spots(attraction_id: int, db: Session = Depends(get_db)) -> list[SpotOut]:
    """景点内部的主要点位：坐标（高德搜不到时为空）+ 怎么玩 / 注意什么。

    数据是 seed 阶段预存的（`trip_spot` 表）—— 这类信息 2~3 年不变，
    不该每次规划都让模型现写。单独一个接口是为了不让景点列表响应变肥。
    """
    return service.list_spots(db, attraction_id)


@router.post("/preview", response_model=PlanPreview, summary="紧凑度预估（不调 LLM）")
def post_preview(payload: PlanRequest, db: Session = Depends(get_db)) -> PlanPreview:
    """选景点时实时调用：这些景点按这个天数排，会有多紧、哪些会被舍弃。"""
    _guard(payload)
    return service.preview_plan(db, payload)


@router.post("/plan", response_model=PlanResponse, summary="生成按天行程方案")
def post_plan(payload: PlanRequest, db: Session = Depends(get_db)) -> PlanResponse:
    _guard(payload)
    try:
        return service.generate_plan(db, payload)
    except HTTPException:
        raise
    except llm.LLMError as exc:
        # 有 Key 但模型彻底不可用，且规则引擎也异常时的最后一道
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成行程失败")
        raise HTTPException(
            status_code=500, detail=f"生成行程失败：{type(exc).__name__}: {exc}"
        ) from exc


@router.post(
    "/auto-plan",
    response_model=PlanResponse,
    summary="一键 AI：只给城市 + 天数 + 节奏，自动挑景点并生成",
)
def post_auto_plan(
    payload: AutoPlanRequest, db: Session = Depends(get_db)
) -> PlanResponse:
    """一键 AI 入口。

    与 `POST /plan` 的唯一区别：**不需要传 `attraction_ids`**，景点由服务端按
    天数与节奏自动挑（必去优先、其次热度）。挑完走的是同一条生成链路，
    因此返回结构与降级行为完全一致；实际选中的景点在 `request.attraction_ids` 里。
    """
    try:
        return service.generate_auto_plan(db, payload)
    except HTTPException:
        raise
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("一键 AI 生成失败")
        raise HTTPException(
            status_code=500, detail=f"一键 AI 生成失败：{type(exc).__name__}: {exc}"
        ) from exc


@router.post(
    "/plan/{plan_id}/adjust",
    response_model=PlanResponse,
    summary="按倾向微调已有方案（不调 LLM）",
)
def post_adjust(
    plan_id: int, payload: PlanRequest, db: Session = Depends(get_db)
) -> PlanResponse:
    """复用原方案的分天，只按新的天数/时间/倾向重排时间轴。

    与「重新生成」的区别：微调是纯硬编码（毫秒级、不花 token），文案沿用原来的；
    重新生成会重走 LLM 流水线。
    """
    _guard(payload)
    try:
        return service.adjust_plan(db, plan_id, payload)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("微调失败")
        raise HTTPException(
            status_code=500, detail=f"微调失败：{type(exc).__name__}: {exc}"
        ) from exc


@router.get("/plans", response_model=list[PlanBrief], summary="历史规划列表")
def get_plans(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[PlanBrief]:
    return service.list_plans(db, limit)


@router.get("/plan/{plan_id}", response_model=PlanResponse, summary="读取某次规划")
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> PlanResponse:
    return service.get_plan(db, plan_id)
