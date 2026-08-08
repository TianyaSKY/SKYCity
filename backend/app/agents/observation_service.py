"""Observation builder: one Chinese text block per agent decision.

Sections follow docs/agent-prompt.md §1 (world state, self, visible locations,
visible people, available actions, last tool result, memory stub) and are
bounded to ~2000 characters. The 上次工具结果 section uses the stable markers
``结果: 成功/失败（…）`` that the fake provider parses.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyInventory,
    EmploymentContract,
    JobApplication,
    JobOpening,
    LeaveRequest,
    Position,
    WorkShift,
)
from app.database.models.conversations import ConversationMessage
from app.database.models.crops import Crop
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Job
from app.database.models.llm_runs import LLMRun
from app.database.models.locations import WorldLocation
from app.database.models.stocks import Stock, StockHolding
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.config.gameplay import (
    HOTEL_NIGHTLY_FEE,
    MANAGER_PROFIT_SHARE_PERCENT,
    MINUTES_PER_STEP,
    OBSERVATION_MAX_CHARS,
    OBSERVATION_MAX_SHOP_PRODUCTS,
    OBSERVATION_MAX_UNREAD_MESSAGES,
    SHIFT_EARLY_WINDOW,
    SHIFT_LATE_LIMIT,
    SLEEP_ENERGY_PER_HOUR,
    SLEEP_MAX_MINUTES,
    SLEEP_MIN_MINUTES,
    SLEEP_MOOD_PER_HOUR,
    STALL_MAX_DISTANCE,
    WAIT_MAX_MINUTES,
    WAIT_MIN_MINUTES,
    WEATHER_MULTIPLIERS,
)
from app.services.action_execution_service import find_path
from app.services.seed_loader import load_blueprints, load_companies, load_crops, load_jobs
from app.world_engine.engine import is_location_open

_WEATHER_NAMES = {
    "clear": "晴朗",
    "cloudy": "多云",
    "rain": "下雨",
    "snow": "下雪",
}

_HOUR_WORD = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜")


def _time_word(world_time: int) -> str:
    hour = (world_time % 1440) // 60
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "早上"
    if hour < 12:
        return "上午"
    if hour == 12:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 20:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def _format_clock(world_time: int) -> str:
    minutes = world_time % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _action_text(agent: Agent, world_time: int) -> str:
    """Human-readable description of an agent's current action (or 空闲)."""
    if agent.action_type is None:
        return "空闲"
    if agent.action_type == "move":
        data = agent.action_data or {}
        return f"正在前往 {data.get('to') or '某地'}（{data.get('reason') or '无理由'}）"
    if agent.action_type == "wait":
        data = agent.action_data or {}
        remaining = (agent.action_ends_at or world_time) - world_time
        return f"等待中（{data.get('reason') or '休息'}，剩余 {max(remaining, 0)} 分钟）"
    if agent.action_type == "sleep":
        data = agent.action_data or {}
        remaining = (agent.action_ends_at or world_time) - world_time
        return f"睡觉中（{data.get('reason') or '休息'}，剩余 {max(remaining, 0)} 分钟）"
    if agent.action_type == "build":
        data = agent.action_data or {}
        remaining = (agent.action_ends_at or world_time) - world_time
        return f"正在建造（{data.get('blueprint_id') or '建筑'}，剩余 {max(remaining, 0)} 分钟）"
    if agent.action_type == "talk":
        # E-full: the conversation lock occupies the agent; the label shows
        # the remaining lock budget so the LLM knows it cannot act yet.
        remaining = (agent.action_ends_at or world_time) - world_time
        return f"对话中（剩余 {max(remaining, 0)} 分钟）"
    return agent.action_type


def build_observation(
        world_id: str,
        agent_id: str,
        session_factory: sessionmaker[Session],
        memory_service: Any = None,
        home_id: str | None = None,
        engine: Any = None,
) -> str:
    """Compose the observation text for one agent decision.

    ``memory_service`` (M6) enables the real 【相关记忆】 section: up to 4
    retrieved memories weighted by entity/keyword/importance/recency. When
    None (no memory system wired) the legacy stub is emitted instead.

    ``home_id`` (R14 sleep steering): the agent's home location id from its
    character card, or None for homeless agents (sleeping requires the hotel).

    ``engine`` (M18): the WorldEngine singleton, used for the 可开店位置
    section's walkability/reachability checks; when None (direct test calls
    without a wired shop service) that section only lists free stalls.
    """
    session = session_factory()
    try:
        world = session.get(World, world_id)
        if world is None:
            return "（世界不存在）"
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        if agent is None:
            return "（智能体不存在）"
        locations = list(
            session.scalars(
                select(WorldLocation)
                .where(WorldLocation.world_id == world_id)
                .order_by(WorldLocation.location_id)
            )
        )
        others = list(
            session.scalars(
                select(Agent)
                .where(Agent.world_id == world_id, Agent.agent_id != agent_id)
                .order_by(Agent.agent_id)
            )
        )
        last_run = session.scalars(
            select(LLMRun)
            .where(LLMRun.world_id == world_id, LLMRun.agent_id == agent_id)
            .order_by(LLMRun.created_at.desc(), LLMRun.run_id.desc())
            .limit(1)
        ).first()
        world_time = world.world_time
        weather = _WEATHER_NAMES.get(world.weather, world.weather or "晴朗")

        location_by_id = {loc.location_id: loc for loc in locations}
        current_loc = location_by_id.get(agent.location_id)
        if current_loc is not None:
            open_state = (
                "开门"
                if is_location_open(
                    current_loc.location_type,
                    current_loc.open_hour,
                    current_loc.close_hour,
                    world_time,
                )
                else "关门"
            )
            here = f"{current_loc.name}（{open_state}）"
        else:
            here = f"空地({agent.col},{agent.row})"

        lines: list[str] = []
        lines.append(
            f"【世界现状】第{world_time // 1440 + 1}天 {_time_word(world_time)} "
            f"{_format_clock(world_time)} 天气: {weather}"
        )
        if home_id is not None and home_id in location_by_id:
            home_name = location_by_id[home_id].name
        elif home_id is not None:
            home_name = home_id  # card home missing from the map: treat as hotel
            home_id = None
        else:
            home_name = None
        home_text = (
            f" 家: {home_name}"
            if home_id is not None
            else f" 无家（睡觉需去小镇旅店，每晚{HOTEL_NIGHTLY_FEE}金币）"
        )
        money_text = f"金钱: {agent.money}"
        if agent.money < 0:
            money_text += f"（负债 {-agent.money} 金币：负债期间不能购物/住店/买股票/转账，尽快打工赚钱还清）"
        lines.append(
            f"【自身状态】饱食度: {agent.satiety}/100 精力: {agent.energy}/100 心情: {agent.mood}/100 "
            f"孤单: {agent.loneliness}/100 {money_text} 所在位置: {here}（格 {agent.col},{agent.row}）"
            f" 当前行动: {_action_text(agent, world_time)}{home_text}"
        )

        # M5: the agent's backpack, ordered by item id.
        inventory_rows = list(
            session.scalars(
                select(Inventory)
                .where(Inventory.world_id == world_id, Inventory.agent_id == agent_id)
                .order_by(Inventory.item_id)
            )
        )
        item_names = {
            item.item_id: item.name
            for item in session.scalars(select(Item).where(Item.world_id == world_id))
        }
        lines.append("【背包】")
        if inventory_rows:
            for row in inventory_rows:
                lines.append(
                    f"- {item_names.get(row.item_id, row.item_id)}（{row.item_id}）×{row.quantity}"
                )
        else:
            lines.append("（空）")

        # M4: unread talk messages for this agent, newest unread first. The
        # line carries the sender's agent id so providers can reply without a
        # name->id lookup; once shown they are marked read (the agent saw them).
        unread = list(
            session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.world_id == world_id,
                    ConversationMessage.to_agent_id == agent_id,
                    ConversationMessage.read.is_(False),
                )
                .order_by(
                    ConversationMessage.sent_at.desc(),
                    ConversationMessage.message_id.desc(),
                )
                .limit(OBSERVATION_MAX_UNREAD_MESSAGES)
            )
        )
        name_by_id = {a.agent_id: a.name for a in others}
        lines.append("【收到的消息】")
        if unread:
            for row in unread:
                sender_name = name_by_id.get(row.from_agent_id, row.from_agent_id)
                lines.append(
                    f"- {sender_name}（{row.from_agent_id}, {row.intent}）：{row.message}"
                )
            for row in unread:
                row.read = True
            session.commit()  # the agent has seen these messages
        else:
            lines.append("（没有新消息）")

        lines.append("【可见地点】")
        # R6 travel cost: BFS steps × MINUTES_PER_STEP × weather multiplier.
        # Shown per location so the agent can weigh time before moving.
        walkable = (
            engine.effective_walkable(session, world_id) if engine is not None else None
        )
        speed_multiplier = WEATHER_MULTIPLIERS.get(world.weather, 1.0)
        for loc in locations:
            open_state = (
                "开门"
                if is_location_open(loc.location_type, loc.open_hour, loc.close_hour, world_time)
                else "关门"
            )
            mark = "（当前位置）" if loc.location_id == agent.location_id else ""
            if loc.location_id != agent.location_id and walkable is not None:
                path = find_path((agent.col, agent.row), (loc.col, loc.row), walkable)
                if path is None:
                    cost_text = "，无法到达"
                else:
                    minutes = max(len(path) - 1, 0) * MINUTES_PER_STEP * speed_multiplier
                    cost_text = f"，路程约{int(minutes)}分钟"
            else:
                cost_text = ""
            lines.append(f"- {loc.name}({loc.location_id}): {open_state}{mark}{cost_text}")

        # M18 R39: where this agent could open a personal shop — free map
        # stalls plus nearby wild cells that are walkable and reachable.
        lines.append("【可开店位置】")
        open_spots: list[str] = []
        for loc in locations:
            if loc.location_type != "stall":
                continue
            if (
                session.scalar(
                    select(Store).where(
                        Store.world_id == world_id,
                        Store.location_id == loc.location_id,
                    )
                )
                is not None
            ):
                continue  # already taken
            open_spots.append(
                f"- 摊位 {loc.name}({loc.location_id}): 营业 {loc.open_hour}~{loc.close_hour}，空置可开店"
            )
        shop = getattr(engine, "shop_service", None) if engine is not None else None
        if shop is not None:
            near: list[str] = []
            for dc in range(-STALL_MAX_DISTANCE, STALL_MAX_DISTANCE + 1):
                for dr in range(-STALL_MAX_DISTANCE, STALL_MAX_DISTANCE + 1):
                    if abs(dc) + abs(dr) > STALL_MAX_DISTANCE:
                        continue
                    col, row = agent.col + dc, agent.row + dr
                    if shop._cell_available(session, world_id, col, row) and shop._reachable(
                            session, world_id, col, row
                    ):
                        near.append(f"- 空地 ({col},{row}): 可开店")
            open_spots.extend(near[:8])
        if open_spots:
            lines.extend(open_spots)
        else:
            lines.append("（暂无空摊位或可达空地）")

        same_location = [a for a in others if a.location_id == agent.location_id]
        lines.append("【可见人物】")
        if same_location:
            for other in same_location:
                # agent_id is required verbatim for talk's target_agent_id —
                # the model cannot derive it from the Chinese name.
                lines.append(f"- {other.name}（{other.agent_id}）: {_action_text(other, world_time)}")
        else:
            lines.append("（当前地点没有其他人）")

        lines.append("【可做的事】")
        lines.append(
            "- move(destination_id, reason): 移动到可见地点中的某个 id"
            "（路程耗时见上方地点列表，雨雪天更慢）"
        )
        lines.append(f"- wait(minutes, reason): 原地等待 {WAIT_MIN_MINUTES}~{WAIT_MAX_MINUTES} 分钟")
        lines.append(
            f"- sleep(minutes, reason): 睡觉 {SLEEP_MIN_MINUTES}~{SLEEP_MAX_MINUTES} 分钟，每小时恢复 "
            f"{SLEEP_ENERGY_PER_HOUR} 点精力、{SLEEP_MOOD_PER_HOUR} 点心情"
            "（比 wait 快）；有家→必须在家睡觉，无家→必须去小镇旅店(village_hotel)"
            f"（每晚 {HOTEL_NIGHTLY_FEE} 金币）"
        )
        if same_location:
            lines.append(
                "- talk(target_agent_id, message, intent): 与【可见人物】中的人对话，"
                "target_agent_id 必须用其括号里的完整 id"
            )
            lines.append(
                "- transfer_money(target_agent_id, amount, reason): 给【可见人物】里的智能体转账金币（需在附近）"
            )
            lines.append(
                "- give_item(target_agent_id, item_id, quantity=1, reason): 把背包里的物品送给【可见人物】里的智能体"
            )

        # M18: personal-shop tools are town-wide (like sell_item), not tied
        # to other agents being around.
        lines.append(
            "- open_shop(location, products, reason): 在空摊位或附近可达空地开店"
            "（资本 ≥100 金币，商品从背包上架，≤3 种，价格 1~2 倍基准价；"
            "可选 buy_price 0~1 倍基准价=收购价，0 不收购）"
        )
        lines.append("- stock_shop(store_id, item_id, quantity=1, reason): 给自己店铺的货架补货（从背包上架）")
        lines.append("- adjust_price(store_id, item_id, new_price, reason): 调整自己店铺的售价")
        lines.append("- set_buy_price(store_id, item_id, new_price, reason): 设置自己店铺的收购价（0~1 倍基准价，0=不收购）")
        lines.append("- close_shop(store_id, reason): 收掉自己的店铺，货架货物退回背包")

        # M5: shop products at the current store (up to 6) + jobs offered here.
        if agent.location_id is not None:
            stores = session.scalars(
                select(Store).where(
                    Store.world_id == world_id, Store.location_id == agent.location_id
                )
            ).all()
            for store in stores:
                products = session.scalars(
                    select(StoreProduct)
                    .where(StoreProduct.world_id == world_id, StoreProduct.store_id == store.store_id)
                    .order_by(StoreProduct.item_id)
                    .limit(OBSERVATION_MAX_SHOP_PRODUCTS)
                ).all()
                for product in products:
                    item = session.get(
                        Item, {"world_id": world_id, "item_id": product.item_id}
                    )
                    tags: list[str] = []
                    if item is not None:
                        if item.satiety_restore > 0:
                            tags.append(f"饱食+{item.satiety_restore}")
                        if item.mood_restore > 0:
                            tags.append(f"心情{item.mood_restore}")
                        if item.work_bonus > 0:
                            tags.append(f"工资+{item.work_bonus}%")
                        elif item.work_bonus_jobs:
                            tags.append("专长工具")  # M19: per-job bonus
                        if item.yield_bonus > 0:
                            tags.append("增产")
                    if product.sell_price < product.base_sell_price:
                        tags.append("促销")
                    suffix = f"（{' '.join(tags)}）" if tags else ""
                    lines.append(
                        f"- buy_item({product.item_id}): {item_names.get(product.item_id, product.item_id)} "
                        f"{product.sell_price}金币（库存{product.stock}）{suffix}"
                    )
            jobs = session.scalars(
                select(Job).where(
                    Job.world_id == world_id, Job.location_id == agent.location_id
                )
            ).all()
            formal_only = {
                seed["job_id"] for seed in load_jobs() if seed.get("formal_only")
            }
            for job in jobs:
                if job.job_id in formal_only:
                    continue  # M16: production recipes run as formal shifts only
                lines.append(
                    f"- work({job.job_id}): {job.name}，{job.duration_minutes}分钟，工资{job.wage}金币"
                )
        lines.append("- sell_item(item_id, quantity, reason): 把背包里的物品卖给商店换钱")
        lines.append("- use_item(item_id, reason): 食用背包里的食物提高饱食度")

        # M14: build blueprints (R22) — always visible, like the stock market:
        # construction changes the town for everyone. Paving blueprints (R24)
        # get their own hint: they open new walkable ground.
        lines.append("【可建造的蓝图】")
        for blueprint in load_blueprints():
            materials = "、".join(
                f"{item_names.get(item_id, item_id)}×{quantity}"
                for item_id, quantity in blueprint.materials.items()
            )
            if blueprint.paving:
                lines.append(
                    f"- build(col, row, {blueprint.blueprint_id}, reason): {blueprint.name}"
                    f"（{blueprint.duration_minutes}分钟，需{materials}；"
                    f"把草地/空地铺成可走的路，目标格需离你 ≤ 3 格且当前不可走）"
                )
                continue
            lines.append(
                f"- build(col, row, {blueprint.blueprint_id}, reason): {blueprint.name}"
                f"（{blueprint.duration_minutes}分钟，需{materials}；"
                f"{'会挡住通行' if blueprint.blocking else '不挡路'}，"
                f"目标格需离你 ≤ 3 格且可行走）"
            )
        lines.append("- 建造完成后建筑会一直留在小镇地图上，所有人都要绕开它走")
        lines.append("- 铺好的路会一直留在小镇地图上，所有人都可以走它去更远的地方")

        # M15: plantable seeds + nearby crops (R23). The farmer sees what it
        # can sow and what is growing near it.
        lines.append("【可种植的种子】")
        for crop_def in load_crops():
            seed_name = item_names.get(crop_def.seed_item_id, crop_def.seed_item_id)
            yield_text = "、".join(
                f"{item_names.get(iid, iid)}×{qty}" for iid, qty in crop_def.yield_items
            )
            lines.append(
                f"- plant(col, row, {crop_def.seed_item_id}, reason): 种{crop_def.name}"
                f"（约{crop_def.total_minutes}分钟成熟，收成{yield_text}）"
            )
        lines.append("- harvest(col, row, reason): 收获附近已成熟的作物")
        crops_by_id = {c.seed_item_id: c for c in load_crops()}
        nearby_crops = [
            row
            for row in session.scalars(
                select(Crop).where(
                    Crop.world_id == world_id,
                    func.abs(Crop.col - agent.col) + func.abs(Crop.row - agent.row) <= 5,
                )
            ).all()
        ]
        if nearby_crops:
            lines.append("【附近作物】")
            for row in nearby_crops:
                crop_def = crops_by_id.get(row.item_id)
                final = len(crop_def.stages) - 1 if crop_def is not None else 0
                state = "成熟可收" if row.stage >= final else f"生长中（阶段{row.stage + 1}/{final + 1}）"
                lines.append(f"- ({row.col},{row.row}) {item_names.get(row.item_id, row.item_id)}：{state}")

        # M10: town-wide stock quotes + own holdings (always visible: the
        # market is village news, not tied to the agent's location).
        lines.append("【股票行情】")
        stocks = session.scalars(
            select(Stock).where(Stock.world_id == world_id).order_by(Stock.stock_id)
        ).all()
        holdings = {
            h.stock_id: h
            for h in session.scalars(
                select(StockHolding).where(
                    StockHolding.world_id == world_id,
                    StockHolding.agent_id == agent.agent_id,
                )
            ).all()
        }
        for stock in stocks:
            delta = stock.price - stock.prev_price
            line = (
                f"- buy_stock({stock.stock_id}, reason, shares=1): {stock.name} 现价{stock.price}金币"
                f"（昨收{stock.prev_price}，{'涨+' if delta >= 0 else '跌'}{abs(delta)}）"
            )
            holding = holdings.get(stock.stock_id)
            if holding is None or holding.shares <= 0:
                line += "——你持有 0 股"
            else:
                profit = stock.price - holding.avg_cost
                line += (
                    f"——你持有 {holding.shares} 股（成本 {holding.avg_cost}金币/股，"
                    f"{'浮盈' if profit >= 0 else '浮亏'}{abs(profit)}金币/股）"
                )
            lines.append(line)
        lines.append("- sell_stock(stock_id, reason, shares=1): 卖出持股变现（不能超卖）")
        lines.append("- 股价每小时随商店/农场经营变动，每日按业绩分红（分红到账看 money_changed）")

        # M13: public job board (R23) + own pending applications (R24). The
        # board is village news, visible everywhere (first version).
        openings = session.execute(
            select(JobOpening, Position, Company)
            .join(
                Position,
                (Position.world_id == JobOpening.world_id)
                & (Position.position_id == JobOpening.position_id),
            )
            .join(
                Company,
                (Company.world_id == JobOpening.world_id)
                & (Company.company_id == JobOpening.company_id),
            )
            .where(JobOpening.world_id == world_id, JobOpening.status == "open")
            .order_by(Company.company_id, Position.position_id)
        ).all()
        if openings:
            lines.append("【公开招聘】")
            for opening, position, company in openings[:6]:
                lines.append(
                    f"- {company.name}：{position.title}，{position.wage_per_shift}金币/班，"
                    f"{_format_clock(position.shift_start_minute)}–{_format_clock(position.shift_end_minute)}，"
                    f"剩余{opening.vacancies}个名额 —— "
                    f"apply_job({opening.opening_id}, reason)"
                )
            lines.append(
                "- apply_job(opening_id, reason): 申请公开招聘中的职位"
                "（opening_id 用上面括号里的完整 id；录用与否由经理决定）"
            )
        my_applications = session.scalars(
            select(JobApplication).where(
                JobApplication.world_id == world_id,
                JobApplication.agent_id == agent.agent_id,
                JobApplication.status == "submitted",
            )
        ).all()
        if my_applications:
            lines.append("【我的申请】")
            for row in my_applications:
                lines.append(
                    f"- {row.company_id}：等待审核 —— "
                    f"withdraw_job_application({row.application_id}, reason)"
                )
            lines.append(
                "- withdraw_job_application(application_id, reason): 撤回我的求职申请"
            )
        # M13: manager desk (R25): own companies, pending reviews.
        managed = session.scalars(
            select(Company).where(
                Company.world_id == world_id,
                Company.manager_agent_id == agent.agent_id,
            )
        ).all()
        if managed:
            lines.append("【企业经营】")
            managed_ids = [company.company_id for company in managed]
            pending_leaves = list(session.scalars(
                select(LeaveRequest).where(
                    LeaveRequest.world_id == world_id,
                    LeaveRequest.company_id.in_(managed_ids),
                    LeaveRequest.status == "pending",
                )
            ).all())
            for company in managed:
                employee_count = int(session.scalar(
                    select(func.count()).select_from(EmploymentContract).where(
                        EmploymentContract.world_id == world_id,
                        EmploymentContract.company_id == company.company_id,
                        EmploymentContract.status.in_(("active", "on_leave")),
                    )
                ) or 0)
                pending = list(session.scalars(
                    select(JobApplication).where(
                        JobApplication.world_id == world_id,
                        JobApplication.company_id == company.company_id,
                        JobApplication.status == "submitted",
                    )
                ).all())
                title_by_position = {
                    position.position_id: position.title
                    for position in session.scalars(
                        select(Position).where(
                            Position.world_id == world_id,
                            Position.company_id == company.company_id,
                        )
                    ).all()
                }
                lines.append(
                    f"- {company.name}（{company.company_id}）：余额{company.money}金币，"
                    f"员工{employee_count}人，欠薪{company.unpaid_wage_total}金币，"
                    f"待审核申请{len(pending)}条，待审批请假"
                    f"{sum(1 for r in pending_leaves if r.company_id == company.company_id)}条"
                )
                lines.append(
                    f"- 每日 00:00 你会按当日净利润的 "
                    f"{MANAGER_PROFIT_SHARE_PERCENT}% 获得经理分成"
                    f"（公司亏损或金库不足则不发）"
                )
                # M16: warehouse + procurement + shelf visibility for the
                # manager's own company (fixed server prices, full IDs).
                inventory_rows = session.scalars(
                    select(CompanyInventory)
                    .where(
                        CompanyInventory.world_id == world_id,
                        CompanyInventory.company_id == company.company_id,
                    )
                    .order_by(CompanyInventory.item_id)
                    .limit(8)
                ).all()
                if inventory_rows:
                    lines.append("  【仓库库存】")
                    for row in inventory_rows:
                        lines.append(
                            f"  - {item_names.get(row.item_id, row.item_id)}"
                            f"（{row.item_id}）"
                            f" 总量{row.quantity}/预留{row.reserved_quantity}/"
                            f"可用{row.quantity - row.reserved_quantity}"
                        )
                company_seed = next(
                    (s for s in load_companies() if s["company_id"] == company.company_id),
                    None,
                )
                for rule in (company_seed or {}).get("procurement") or []:
                    seller_id = str(rule.get("seller_company_id") or "")
                    seller = session.get(
                        Company, {"world_id": world_id, "company_id": seller_id}
                    )
                    seller_name = seller.name if seller is not None else seller_id
                    rule_item = str(rule.get("item_id") or "")
                    lines.append(
                        f"  - 可采购：从 {seller_name}（{seller_id}）采购 "
                        f"{item_names.get(rule_item, rule_item)}（{rule_item}），"
                        f"{rule.get('unit_price')}金币/件 —— "
                        f"purchase_company_goods({company.company_id}, {seller_id}, "
                        f"{rule_item}, reason, quantity=N)"
                    )
                for store in session.scalars(
                        select(Store).where(
                            Store.world_id == world_id,
                            Store.company_id == company.company_id,
                        )
                ).all():
                    for product in session.scalars(
                            select(StoreProduct)
                            .where(
                                StoreProduct.world_id == world_id,
                                StoreProduct.store_id == store.store_id,
                            )
                            .order_by(StoreProduct.item_id)
                    ).all():
                        if product.stock >= product.stock_cap:
                            continue
                        lines.append(
                            f"  - 可上架：{item_names.get(product.item_id, product.item_id)}"
                            f"（{product.item_id}）货架{product.stock}/{product.stock_cap} —— "
                            f"stock_store({company.company_id}, {store.store_id}, "
                            f"{product.item_id}, reason, quantity=N)"
                        )
                if pending:
                    lines.append("  【待审核求职申请】")
                    for row in pending[:3]:
                        applicant = session.get(
                            Agent,
                            {"world_id": world_id, "agent_id": row.agent_id},
                        )
                        applicant_name = applicant.name if applicant is not None else row.agent_id
                        lines.append(
                            f"  - {applicant_name}（{row.agent_id}）申请"
                            f"{title_by_position.get(row.position_id, row.position_id)}："
                            f"{row.applicant_reason} —— "
                            f"review_job_application({row.application_id}, accept|reject, reason)"
                        )
                    lines.append(
                        "- review_job_application(application_id, accept|reject, reason): "
                        "审核求职申请（仅企业经理）"
                    )
            if pending_leaves:
                lines.append("  【待审批请假】")
                for request in pending_leaves[:3]:
                    applicant = session.get(
                        Agent, {"world_id": world_id, "agent_id": request.agent_id}
                    )
                    applicant_name = applicant.name if applicant is not None else request.agent_id
                    lines.append(
                        f"  - {applicant_name}（{request.agent_id}）：{request.reason} —— "
                        f"review_leave_request({request.request_id}, approve|reject, reason)"
                    )
                lines.append(
                    "- review_leave_request(request_id, approve|reject, reason): "
                    "审批请假（仅企业经理；准假不判缺勤也不发工资）"
                )
            position_rows = session.scalars(
                select(Position).where(
                    Position.world_id == world_id,
                    Position.company_id.in_(managed_ids),
                )
            ).all()
            for position in position_rows:
                lines.append(
                    f"- {'pause' if position.status == 'active' else 'resume'}_recruitment("
                    f"{position.position_id}, reason): "
                    f"{'暂停' if position.status == 'active' else '恢复'}{position.title}招聘"
                )
            employee_contracts = session.scalars(
                select(EmploymentContract).where(
                    EmploymentContract.world_id == world_id,
                    EmploymentContract.company_id.in_(managed_ids),
                    EmploymentContract.status.in_(("active", "on_leave")),
                )
            ).all()
            for contract_row in employee_contracts[:3]:
                employee = session.get(
                    Agent,
                    {"world_id": world_id, "agent_id": contract_row.agent_id},
                )
                employee_name = employee.name if employee is not None else contract_row.agent_id
                lines.append(
                    f"- terminate_employment({contract_row.employment_id}, reason): "
                    f"解雇 {employee_name}（{contract_row.agent_id}）"
                )
            lines.append(
                "- pause_recruitment/resume_recruitment/terminate_employment 仅企业经理可用；"
                "解雇不消除欠薪"
            )
            lines.append(
                "- purchase_company_goods(buyer_company_id, seller_company_id, item_id, "
                "reason, quantity=1): 按固定价向其他企业采购（仅企业经理，价格由服务器决定）"
            )
            lines.append(
                "- stock_store(company_id, store_id, item_id, reason, quantity=1): "
                "把本企业仓库货物上架到自有商店货架（仅企业经理，需货架有空间）"
            )

        # M18 R41: the agent's own personal shops (owner view) — products
        # and cumulative sales (from the sale_income ledger).
        my_stores = session.scalars(
            select(Store)
            .where(
                Store.world_id == world_id,
                Store.owner_agent_id == agent.agent_id,
            )
            .order_by(Store.store_id)
        ).all()
        if my_stores:
            total_sales = (
                session.scalar(
                    select(func.sum(Transaction.amount)).where(
                        Transaction.world_id == world_id,
                        Transaction.agent_id == agent.agent_id,
                        Transaction.type == "sale_income",
                    )
                )
                or 0
            )
            lines.append("【店铺经营摘要】")
            for store in my_stores:
                store_name = store.name or store.store_id
                lines.append(f"- {store_name}({store.store_id}): 累计销售额 {total_sales}")
                for product in session.scalars(
                    select(StoreProduct)
                    .where(
                        StoreProduct.world_id == world_id,
                        StoreProduct.store_id == store.store_id,
                    )
                    .order_by(StoreProduct.item_id)
                ).all():
                    lines.append(
                        f"  - {item_names.get(product.item_id, product.item_id)} "
                        f"{product.sell_price}金币（库存{product.stock}/{product.stock_cap}）"
                    )
        else:
            lines.append("（暂无店铺）")

        # M13: employee card (R27/R31): own contract + today's shift.
        contract = session.scalar(
            select(EmploymentContract).where(
                EmploymentContract.world_id == world_id,
                EmploymentContract.agent_id == agent.agent_id,
                EmploymentContract.status.in_(("active", "on_leave")),
            )
        )
        if contract is not None:
            company = session.get(
                Company, {"world_id": world_id, "company_id": contract.company_id}
            )
            position = session.get(
                Position, {"world_id": world_id, "position_id": contract.position_id}
            )
            company_name = company.name if company is not None else contract.company_id
            position_title = position.title if position is not None else contract.position_id
            lines.append("【正式职业】")
            lines.append(
                f"- {company_name}：{position_title}，每班{contract.wage_per_shift}金币，"
                f"出勤评分{contract.attendance_score:.0f}，"
                f"未发工资{contract.unpaid_wage}金币"
            )
            if contract.unpaid_wage > 0:
                lines.append(f"  企业尚欠你{contract.unpaid_wage}金币工资。")
            upcoming_shift = session.scalar(
                select(WorkShift).where(
                    WorkShift.world_id == world_id,
                    WorkShift.employment_id == contract.employment_id,
                    WorkShift.scheduled_start >= world_time - 120,
                    WorkShift.status.in_(("scheduled", "in_progress", "late")),
                )
                .order_by(WorkShift.scheduled_start)
                .limit(1)
            )
            if upcoming_shift is not None:
                minutes_until = max(upcoming_shift.scheduled_start - world_time, 0)
                status_text = (
                    "尚未签到"
                    if upcoming_shift.status == "scheduled"
                    else "已签到（工作中）"
                )
                lines.append("【今天班次】")
                lines.append(
                    f"- {_format_clock(upcoming_shift.scheduled_start)}–"
                    f"{_format_clock(upcoming_shift.scheduled_end)}，状态：{status_text}，"
                    f"距开始{minutes_until}分钟，地点{company_name}（{company.location_id}）"
                )
                if upcoming_shift.status == "scheduled":
                    lines.append(
                        f"- start_shift({upcoming_shift.shift_id}, reason): 到达工作地点后签到"
                        f"开始班次（可提前{SHIFT_EARLY_WINDOW}分钟，迟到上限{SHIFT_LATE_LIMIT}分钟）"
                    )
                    lines.append(
                        f"- request_leave({upcoming_shift.shift_id}, reason): 无法到岗时请假"
                        "（经理审批，准假不判缺勤）"
                    )
            lines.append(
                f"- resign_job({contract.employment_id}, reason): 辞去当前正式工作"
                "（未来班次取消、岗位重开、欠薪保留）"
            )

        lines.append("【上次工具结果】")
        if last_run is None:
            lines.append("（无）")
        else:
            detail = ""
            tool_result = last_run.tool_result or {}
            if isinstance(tool_result, dict):
                detail = tool_result.get("reason") or ""
            status = "成功" if last_run.success else "失败"
            arguments = json.dumps(last_run.tool_arguments or {}, ensure_ascii=False)
            lines.append(
                f"工具: {last_run.tool_name or '（无）'} | 参数: {arguments} | "
                f"结果: {status}（{detail or '已执行'}）"
            )

        lines.append("【相关记忆】")
        if memory_service is not None:
            # T6-4 context: current location + nearby agents + own items as
            # entities; weather + current action word as keywords.
            memory_entities: list[str] = []
            if agent.location_id:
                memory_entities.append(agent.location_id)
            memory_entities.extend(other.agent_id for other in same_location)
            memory_entities.extend(row.item_id for row in inventory_rows)
            memory_keywords = [weather]
            if agent.action_type:
                memory_keywords.append(agent.action_type)
            memories = memory_service.retrieve(
                world_id,
                agent_id,
                context_entities=memory_entities,
                context_keywords=memory_keywords,
                limit=4,
                session=session,
                world_time=world_time,
            )
            if memories:
                for memory in memories:
                    text = memory.text.replace("\n", " ")
                    lines.append(
                        f"- [{memory.memory_type}] {text}（重要度 {memory.importance}）"
                    )
            else:
                lines.append("（暂无）")
            session.commit()  # persist the recall bumps with the read marks
        else:
            lines.append("[记忆系统将在后续里程碑启用]")

        observation = "\n".join(lines)
        if len(observation) > OBSERVATION_MAX_CHARS:
            observation = observation[: OBSERVATION_MAX_CHARS - 20] + "\n…[观察已截断]"
        return observation
    finally:
        session.close()
