"""ORM 模型：三张表。

trip_city        —— 预置热门城市
trip_attraction  —— 景点池（坐标统一 GCJ-02）
trip_plan        —— 规划快照（入参 + 出参整体 JSON 存档，V1 不做关系化）

坐标说明：全库统一 GCJ-02（火星坐标系）。
高德 POI 搜索返回的 location 字段即 GCJ-02，可直接入库；
离线种子里手写的坐标也已按 GCJ-02 校准。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class City(Base):
    """预置热门城市。hotel_areas 存该城市的候选住宿片区，供规划时取舍。"""

    __tablename__ = "trip_city"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="城市名，如 成都")
    pinyin: Mapped[str] = mapped_column(String(32), default="", comment="拼音，供前端路由/搜索")
    emoji: Mapped[str] = mapped_column(String(8), default="", comment="宫格图标")
    tagline: Mapped[str] = mapped_column(String(64), default="", comment="一句话卖点")
    heat: Mapped[int] = mapped_column(Integer, default=0, comment="城市热度，宫格排序用")
    center_lat: Mapped[float] = mapped_column(Float, default=0.0, comment="市中心纬度 GCJ-02")
    center_lng: Mapped[float] = mapped_column(Float, default=0.0, comment="市中心经度 GCJ-02")
    hotel_areas: Mapped[list] = mapped_column(
        JSON,
        default=list,
        comment='住宿片区 [{"area":"春熙路/太古里","lat":..,"lng":..,"pros":"..","cons":".."}]',
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    attractions: Mapped[list["Attraction"]] = relationship(
        back_populates="city", cascade="all, delete-orphan"
    )


class Attraction(Base):
    """景点。heat 降序即前端展示顺序。"""

    __tablename__ = "trip_attraction"
    __table_args__ = (
        UniqueConstraint("city_id", "name", name="uq_attraction_city_name"),
        Index("ix_attraction_city_heat", "city_id", "heat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[int] = mapped_column(
        ForeignKey("trip_city.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    intro: Mapped[str] = mapped_column(String(255), default="", comment="一句话简介")
    district: Mapped[str] = mapped_column(String(32), default="", comment="所在区/商圈，用于生成餐饮节点")
    lat: Mapped[float] = mapped_column(Float, nullable=False, comment="GCJ-02 纬度")
    lng: Mapped[float] = mapped_column(Float, nullable=False, comment="GCJ-02 经度")
    visit_minutes: Mapped[int] = mapped_column(Integer, default=90, comment="建议游览时长(分钟)")
    heat: Mapped[int] = mapped_column(Integer, default=50, comment="热度 0-100")
    must_visit: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否城市必去")
    best_time: Mapped[str] = mapped_column(
        String(12),
        default="",
        server_default="",
        comment="最佳时段：morning(早场) / night(夜景) / museum(闭馆早)；空=无约束",
    )
    tags: Mapped[list] = mapped_column(JSON, default=list, comment='["历史","亲子"]')
    coord_source: Mapped[str] = mapped_column(
        String(16), default="seed", comment="坐标来源：amap / seed / manual"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    city: Mapped[City] = relationship(back_populates="attractions")


class TripPlan(Base):
    """一次规划的完整快照。入参与出参都存 JSON，便于回溯与复现。"""

    __tablename__ = "trip_plan"
    __table_args__ = (Index("ix_trip_plan_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String(32), nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    attraction_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(
        String(16), default="llm", comment="生成来源：llm / fallback"
    )
    title: Mapped[str] = mapped_column(String(128), default="", comment="方案标题，历史列表展示")
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False, comment="入参快照")
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, comment="出参快照")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TripPlan #{self.id} {self.city} {self.days}天 {self.source}>"
