"""对外协议模型（Pydantic v2）。

这里同时承担两个角色：
  1. HTTP 请求 / 响应的契约
  2. LLM 输出的强校验 schema —— PlanResult 就是给 DeepSeek 的「必须长这样」的约定，
     校验失败即触发重试，因此字段设计要严但不啰嗦。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------- 枚举
NodeType = Literal["depart", "attraction", "meal", "hotel", "transit"]
Pace = Literal["轻松", "适中", "紧凑"]
# 出行方式三选：
#   drive   自驾（全程自己开）
#   mixed   打车+公共（默认，按距离自动判断该打车还是坐地铁）
#   transit 公共（公交地铁 / 城际大巴 / 城际铁路）
# taxi 是历史值，等价于 mixed（老数据里存的就是它）
Transport = Literal["drive", "mixed", "transit", "taxi"]
# 行程倾向：宽松会舍弃更多景点，紧凑会尽量多塞
PacePref = Literal["relaxed", "balanced", "packed"]


# ---------------------------------------------------------------- 请求
class PlanRequest(BaseModel):
    """POST /api/trip/plan 的入参。"""

    city: str = Field(..., description="城市名或拼音，如 成都 / chengdu")
    attraction_ids: list[int] = Field(..., min_length=1, description="勾选的景点 id")
    days: int = Field(1, ge=1, le=7, description="行程天数 1~7")
    start_time: str = Field("09:00", description="每天出发时间 HH:MM")
    return_time: str = Field("19:30", description="目标返回时间 HH:MM")
    # 首末天常常和中间几天不一样：第一天可能中午才到，最后一天要赶飞机
    first_day_start_time: str | None = Field(
        None, description="第一天到达/出发时间；不填则用 start_time"
    )
    last_day_return_time: str | None = Field(
        None, description="最后一天返回时间；不填则用 return_time"
    )
    transport: Transport = Field("mixed", description="drive=自驾 / mixed=打车+公共 / transit=公共")
    pace: PacePref = Field("balanced", description="relaxed=宽松 / balanced=平衡 / packed=紧凑")
    plan_id_hint: int | None = Field(None, description="重新生成时传入原 id，仅作前端标记")

    @field_validator("start_time", "return_time")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        return _norm_hhmm(v)

    @field_validator("first_day_start_time", "last_day_return_time")
    @classmethod
    def _check_optional_hhmm(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        return _norm_hhmm(v)

    @field_validator("transport", mode="before")
    @classmethod
    def _norm_transport(cls, v):
        # 老数据/老前端传的是 taxi，等价于 mixed
        return "mixed" if v == "taxi" else v

    @field_validator("attraction_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen, out = set(), []
        for i in v:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    @property
    def transport_label(self) -> str:
        return TRANSPORT_LABELS.get(self.transport, self.transport)

    @property
    def pace_label(self) -> str:
        return PACE_LABELS.get(self.pace, self.pace)


class AutoPlanRequest(BaseModel):
    """「一键 AI」入参：只给城市 + 天数 + 节奏必填，景点由服务端自动挑选。

    与 PlanRequest 拆成两个模型，是为了让「勾选生成」与「一键生成」的契约各自清楚、
    互不影响 —— 后者内部先挑景点，再转成 PlanRequest 走同一条生成链路。
    """

    city: str = Field(..., description="城市名或拼音，如 成都 / chengdu")
    days: int = Field(2, ge=1, le=7, description="行程天数 1~7（必填）")
    pace: PacePref = Field("balanced", description="relaxed=宽松 / balanced=平衡 / packed=紧凑（必填）")
    transport: Transport = Field("mixed", description="drive=自驾 / mixed=打车+公共 / transit=公共")
    start_time: str = Field("09:00", description="每天出发时间 HH:MM")
    return_time: str = Field("19:30", description="目标返回时间 HH:MM")
    first_day_start_time: str | None = Field(None, description="第一天到达/出发时间")
    last_day_return_time: str | None = Field(None, description="最后一天返回时间")

    @field_validator("start_time", "return_time")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        return _norm_hhmm(v)

    @field_validator("first_day_start_time", "last_day_return_time")
    @classmethod
    def _check_optional_hhmm(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        return _norm_hhmm(v)

    @field_validator("transport", mode="before")
    @classmethod
    def _norm_transport(cls, v):
        # 老数据/老前端传的是 taxi，等价于 mixed
        return "mixed" if v == "taxi" else v


TRANSPORT_LABELS = {"drive": "自驾", "mixed": "打车+公共", "transit": "公共交通", "taxi": "打车+公共"}
PACE_LABELS = {"relaxed": "宽松", "balanced": "平衡", "packed": "紧凑"}


def _norm_hhmm(v: str) -> str:
    v = str(v).strip().replace("：", ":")
    parts = v.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("时间格式应为 HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("时间超出范围")
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------- LLM 输出契约
class TimelineNode(BaseModel):
    """时间轴上的一个节点。"""

    model_config = ConfigDict(extra="ignore")

    time: str = Field(..., description="到达/开始时间 HH:MM")
    type: NodeType = Field(..., description="节点类型")
    name: str = Field(..., min_length=1, max_length=64)
    advice: str = Field("", max_length=200, description="一句话建议")
    attraction_id: int | None = Field(None, description="景点节点必须回填原始 id")
    stay_minutes: int = Field(0, ge=0, le=600, description="停留时长")
    travel_minutes: int = Field(
        0, ge=0, le=600, description="从上一站到这里的耗时（含可能的等待）"
    )
    travel_mode: str = Field(
        "", max_length=16, description="交通方式中文名：步行/地铁公交/打车/自驾/顺风车/城际大巴/城际铁路"
    )
    must_visit: bool = Field(False, description="是否必去")
    moved_to_day: int | None = Field(
        None, ge=1, le=7, description="该景点被挪到第几天；前端显示『已挪至 Day N』"
    )
    moved_from_day: int | None = Field(None, ge=1, le=7, description="从第几天挪过来的")
    move_reason: str = Field("", max_length=120, description="挪动原因")

    @field_validator("time")
    @classmethod
    def _norm_time(cls, v: str) -> str:
        v = v.strip().replace("：", ":")
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"时间格式非法: {v}")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


class HotelAlternative(BaseModel):
    model_config = ConfigDict(extra="ignore")

    area: str = Field(..., max_length=64)
    tradeoff: str = Field("", max_length=200, description="取舍理由")


class HotelAdvice(BaseModel):
    """住宿片区建议 + 取舍理由。"""

    model_config = ConfigDict(extra="ignore")

    area: str = Field(..., max_length=64)
    reason: str = Field("", max_length=300, description="为什么住这里")
    alternatives: list[HotelAlternative] = Field(default_factory=list)


class DayPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: int = Field(..., ge=1, le=7)
    theme: str = Field("", max_length=80, description="当天主题一句话")
    pace: Pace = Field("适中", description="丰富度")
    nodes: list[TimelineNode] = Field(default_factory=list)
    hotel: HotelAdvice | None = None

    @field_validator("pace", mode="before")
    @classmethod
    def _norm_pace(cls, v):
        # LLM 偶尔输出英文/近义词，做一次归一
        if isinstance(v, str):
            mapping = {
                "relaxed": "轻松", "easy": "轻松", "low": "轻松",
                "moderate": "适中", "medium": "适中", "normal": "适中",
                "packed": "紧凑", "intense": "紧凑", "high": "紧凑", "busy": "紧凑",
            }
            v = mapping.get(v.strip().lower(), v.strip())
        return v


class DroppedItem(BaseModel):
    """被舍弃的景点。"""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., max_length=64)
    reason: str = Field("", max_length=200)
    attraction_id: int | None = None


class MustVisitReason(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., max_length=64)
    reason: str = Field("", max_length=200)
    attraction_id: int | None = None


class PlanResult(BaseModel):
    """最终时间轴。由 planner 物化产出，时间全部是算出来的。"""

    model_config = ConfigDict(extra="ignore")

    city: str = Field(..., max_length=32)
    days: int = Field(..., ge=1, le=7)
    summary: str = Field("", max_length=300, description="整体方案一句话总述")
    day_plans: list[DayPlan] = Field(..., min_length=1)
    must_visit_reasons: list[MustVisitReason] = Field(default_factory=list)
    dropped: list[DroppedItem] = Field(default_factory=list)

    @field_validator("day_plans")
    @classmethod
    def _check_days(cls, v: list[DayPlan]) -> list[DayPlan]:
        if not v:
            raise ValueError("day_plans 不能为空")
        days = [d.day for d in v]
        if sorted(days) != list(range(1, len(v) + 1)):
            raise ValueError(f"day 编号必须从 1 连续递增，实际为 {days}")
        return v


# ---------------------------------------------------------------- 终检契约
class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: int | None = Field(None, ge=1, le=7)
    level: Literal["info", "warn", "error"] = "warn"
    text: str = Field(..., max_length=200)


class PlanReview(BaseModel):
    """LLM 对最终时间轴的复核结果。只提意见，不改时间轴。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = True
    summary: str = Field("", max_length=300, description="复核后润色过的方案总述")
    issues: list[ReviewIssue] = Field(default_factory=list)



# ---------------------------------------------------------------- 响应
class CityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    pinyin: str = ""
    emoji: str = ""
    tagline: str = ""
    heat: int = 0
    center_lat: float = 0.0
    center_lng: float = 0.0
    attraction_count: int = 0


class AttractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    intro: str = ""
    district: str = ""
    lat: float
    lng: float
    visit_minutes: int = 90
    heat: int = 50
    must_visit: bool = False
    best_time: str = Field("", description="最佳时段 morning/night/museum，空=无约束")
    tags: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    """规划出参。attractions 一并回传，前端画地图无需二次请求。"""

    plan_id: int
    created_at: str
    source: Literal["llm", "fallback", "tweak"] = Field(
        ..., description="llm=AI 流水线 / tweak=倾向微调 / fallback=本地规则引擎"
    )
    title: str = ""
    request: PlanRequest
    result: PlanResult
    attractions: list[AttractionOut] = Field(default_factory=list)
    review: PlanReview | None = Field(
        None, description="LLM 终检结果（含审核意见与润色后的总述）"
    )
    trace: list[str] = Field(
        default_factory=list, description="LLM 工具调用轨迹，排查生成过程用"
    )


class PlanBrief(BaseModel):
    """历史列表用的轻量条目。"""

    plan_id: int
    city: str
    days: int
    title: str
    source: str
    created_at: str
    attraction_count: int


class PlanPreview(BaseModel):
    """选景点时的紧凑度预估（纯硬编码，毫秒级，不调 LLM）。

    主指标是 `days_needed`（地理下限 = 片区簇 + 独占型各占一天）与用户设的天数之比；
    `load_ratio` 仅作参考保留，**不再参与判定** —— 它对天数不敏感（分子分母同比例增长），
    算不出「6 天只排了 4 天的量」这种情况。
    """

    city: str
    days: int
    pace: PacePref = "balanced"
    transport: Transport = "mixed"

    selected_count: int
    scheduled_count: int
    total_visit_minutes: int
    total_travel_minutes: int
    capacity_minutes: int
    load_ratio: float

    tightness: Literal["轻松", "适中", "紧凑", "超载"]
    days_needed: int = Field(0, description="按地理下限推算的最少天数")
    per_day: list[dict] = Field(default_factory=list)
    will_drop: list[str] = Field(default_factory=list)
    suggestion: str = ""


class ApiError(BaseModel):
    ok: bool = False
    error: str
    detail: str = ""
