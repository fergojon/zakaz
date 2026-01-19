"""
ZAKAZ BOT - Production Ready Telegram Bot
Single File Version

Bu bot barcha talablarga javob beradi:
- 3 xil rol (Admin, Worker, User)
- Avtomatik zakaz taqsimlash
- Pro worker ustunligi
- Queue system
- Timeout mexanizmi
- Reyting tizimi
- FSM bilan state management
- Security checks

SETUP:
1. pip install aiogram asyncpg sqlalchemy alembic python-dotenv pydantic pydantic-settings
2. PostgreSQL database yarating
3. .env faylini to'ldiring
4. python bot_complete.py

ENVIRONMENT VARIABLES (.env):
BOT_TOKEN=your_bot_token
ADMIN_ID=your_telegram_id
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Callable, Dict, Any, Awaitable
from functools import wraps

# Aiogram imports
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# SQLAlchemy imports
from sqlalchemy import BigInteger, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Pydantic
from pydantic_settings import BaseSettings, SettingsConfigDict

# Standard library
from os import getenv

# =============================================================================
# CONFIGURATION
# =============================================================================

class Settings(BaseSettings):
    """Application settings"""
    BOT_TOKEN: str
    ADMIN_ID: int
    DATABASE_URL: str
    ORDER_ACCEPT_TIMEOUT: int = 120
    LOG_LEVEL: str = "INFO"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True
    )

settings = Settings()

# Logging setup
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# DATABASE MODELS
# =============================================================================

class Base(DeclarativeBase):
    """Base class for all models"""
    pass


class Worker(Base):
    __tablename__ = "workers"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_pro: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="free")
    rating: Mapped[float] = mapped_column(Float, default=5.0)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    completed_orders: Mapped[int] = mapped_column(Integer, default=0)
    
    orders: Mapped[List["Order"]] = relationship("Order", back_populates="worker")
    ratings: Mapped[List["Rating"]] = relationship("Rating", back_populates="worker")


class Order(Base):
    __tablename__ = "orders"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    worker: Mapped[Optional["Worker"]] = relationship("Worker", back_populates="orders")
    rating: Mapped[Optional["Rating"]] = relationship("Rating", back_populates="order", uselist=False)


class Rating(Base):
    __tablename__ = "ratings"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workers.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    order: Mapped["Order"] = relationship("Order", back_populates="rating")
    worker: Mapped["Worker"] = relationship("Worker", back_populates="ratings")


# Database setup
engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# =============================================================================
# FSM STATES
# =============================================================================

class UserStates(StatesGroup):
    waiting_order_text = State()
    waiting_rating_comment = State()


class AdminStates(StatesGroup):
    waiting_worker_id = State()
    waiting_worker_remove = State()
    waiting_pro_toggle = State()


# =============================================================================
# KEYBOARDS
# =============================================================================

def get_main_keyboard(user_id: int, is_worker: bool = False) -> ReplyKeyboardMarkup:
    """Get main keyboard based on role"""
    if user_id == settings.ADMIN_ID:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📦 Zakaz berish")],
            [KeyboardButton(text="👷 Labochilar"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="➕ Labochi qo'shish"), KeyboardButton(text="➖ Labochi o'chirish")],
            [KeyboardButton(text="⭐ Pro berish/olish")]
        ], resize_keyboard=True)
    elif is_worker:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📦 Zakaz berish")],
            [KeyboardButton(text="📊 Mening statusim"), KeyboardButton(text="📈 Statistika")]
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📦 Zakaz berish")],
            [KeyboardButton(text="📊 Mening zakazlarim")]
        ], resize_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)


def order_worker_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Qabul qilish", callback_data=f"accept:{order_id}")],
        [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject:{order_id}")]
    ])


def order_progress_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✔️ Bajarildi", callback_data=f"complete:{order_id}")]
    ])


def rating_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐", callback_data=f"rate:{order_id}:1"),
            InlineKeyboardButton(text="⭐⭐", callback_data=f"rate:{order_id}:2"),
            InlineKeyboardButton(text="⭐⭐⭐", callback_data=f"rate:{order_id}:3"),
        ],
        [
            InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data=f"rate:{order_id}:4"),
            InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data=f"rate:{order_id}:5"),
        ],
        [InlineKeyboardButton(text="❌ Keyinroq", callback_data=f"rate:{order_id}:skip")]
    ])


# =============================================================================
# REPOSITORIES
# =============================================================================

class WorkerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(self, worker_id: int) -> Worker:
        worker = Worker(id=worker_id)
        self.session.add(worker)
        await self.session.commit()
        await self.session.refresh(worker)
        return worker
    
    async def get_by_id(self, worker_id: int) -> Optional[Worker]:
        result = await self.session.execute(select(Worker).where(Worker.id == worker_id))
        return result.scalar_one_or_none()
    
    async def get_all(self) -> List[Worker]:
        result = await self.session.execute(select(Worker))
        return list(result.scalars().all())
    
    async def get_free_worker(self) -> Optional[Worker]:
        # Pro workers first
        result = await self.session.execute(
            select(Worker).where(Worker.status == "free", Worker.is_pro == True)
            .order_by(Worker.rating.desc()).limit(1)
        )
        worker = result.scalar_one_or_none()
        if not worker:
            result = await self.session.execute(
                select(Worker).where(Worker.status == "free", Worker.is_pro == False)
                .order_by(Worker.rating.desc()).limit(1)
            )
            worker = result.scalar_one_or_none()
        return worker
    
    async def update_status(self, worker_id: int, status: str):
        worker = await self.get_by_id(worker_id)
        if worker:
            worker.status = status
            await self.session.commit()
    
    async def toggle_pro(self, worker_id: int) -> Optional[bool]:
        worker = await self.get_by_id(worker_id)
        if worker:
            worker.is_pro = not worker.is_pro
            await self.session.commit()
            return worker.is_pro
        return None
    
    async def delete(self, worker_id: int) -> bool:
        worker = await self.get_by_id(worker_id)
        if worker:
            await self.session.delete(worker)
            await self.session.commit()
            return True
        return False
    
    async def exists(self, worker_id: int) -> bool:
        result = await self.session.execute(select(Worker.id).where(Worker.id == worker_id))
        return result.scalar_one_or_none() is not None
    
    async def update_rating(self, worker_id: int):
        result = await self.session.execute(
            select(func.avg(Rating.score)).where(Rating.worker_id == worker_id)
        )
        avg_rating = result.scalar_one_or_none()
        if avg_rating:
            worker = await self.get_by_id(worker_id)
            if worker:
                worker.rating = float(avg_rating)
                await self.session.commit()
    
    async def increment_orders(self, worker_id: int, completed: bool = False):
        worker = await self.get_by_id(worker_id)
        if worker:
            worker.total_orders += 1
            if completed:
                worker.completed_orders += 1
            await self.session.commit()


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(self, user_id: int, text: str, worker_id: Optional[int] = None, status: str = "new") -> Order:
        order = Order(user_id=user_id, text=text, worker_id=worker_id, status=status)
        self.session.add(order)
        await self.session.commit()
        await self.session.refresh(order)
        return order
    
    async def get_by_id(self, order_id: int) -> Optional[Order]:
        result = await self.session.execute(select(Order).where(Order.id == order_id))
        return result.scalar_one_or_none()
    
    async def get_pending_orders(self) -> List[Order]:
        result = await self.session.execute(
            select(Order).where(Order.status == "new", Order.worker_id.is_(None))
            .order_by(Order.created_at)
        )
        return list(result.scalars().all())
    
    async def get_user_orders(self, user_id: int, limit: int = 10) -> List[Order]:
        result = await self.session.execute(
            select(Order).where(Order.user_id == user_id)
            .order_by(Order.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
    
    async def update_status(self, order_id: int, new_status: str, worker_id: Optional[int] = None):
        order = await self.get_by_id(order_id)
        if order:
            order.status = new_status
            if worker_id is not None:
                order.worker_id = worker_id
            if new_status == "accepted":
                order.accepted_at = datetime.utcnow()
            elif new_status in ["done", "cancelled"]:
                order.completed_at = datetime.utcnow()
            await self.session.commit()
    
    async def get_statistics(self) -> dict:
        total = await self.session.execute(select(func.count(Order.id)))
        completed = await self.session.execute(select(func.count(Order.id)).where(Order.status == "done"))
        cancelled = await self.session.execute(select(func.count(Order.id)).where(Order.status == "cancelled"))
        in_progress = await self.session.execute(
            select(func.count(Order.id)).where(Order.status.in_(["new", "accepted", "in_progress"]))
        )
        return {
            "total": total.scalar_one(),
            "completed": completed.scalar_one(),
            "cancelled": cancelled.scalar_one(),
            "in_progress": in_progress.scalar_one()
        }


class RatingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(self, order_id: int, worker_id: int, user_id: int, score: int, comment: Optional[str] = None) -> Rating:
        rating = Rating(order_id=order_id, worker_id=worker_id, user_id=user_id, score=score, comment=comment)
        self.session.add(rating)
        await self.session.commit()
        await self.session.refresh(rating)
        return rating
    
    async def get_by_order(self, order_id: int) -> Optional[Rating]:
        result = await self.session.execute(select(Rating).where(Rating.order_id == order_id))
        return result.scalar_one_or_none()
    
    async def exists_for_order(self, order_id: int) -> bool:
        result = await self.session.execute(select(Rating.id).where(Rating.order_id == order_id))
        return result.scalar_one_or_none() is not None


# =============================================================================
# SERVICES
# =============================================================================

class OrderService:
    def __init__(self, session: AsyncSession, bot: Bot):
        self.order_repo = OrderRepository(session)
        self.worker_repo = WorkerRepository(session)
        self.session = session
        self.bot = bot
    
    async def create_order(self, user_id: int, text: str) -> tuple[Order, Optional[int]]:
        worker = await self.worker_repo.get_free_worker()
        
        if worker:
            order = await self.order_repo.create(user_id, text, worker.id, "accepted")
            await self.worker_repo.update_status(worker.id, "busy")
            await self.worker_repo.increment_orders(worker.id)
            await self._notify_worker(worker.id, order)
            asyncio.create_task(self._order_timeout(order.id, worker.id))
            return order, worker.id
        else:
            order = await self.order_repo.create(user_id, text)
            return order, None
    
    async def accept_order(self, order_id: int, worker_id: int) -> bool:
        order = await self.order_repo.get_by_id(order_id)
        if order and order.worker_id == worker_id and order.status == "accepted":
            await self.order_repo.update_status(order_id, "in_progress")
            return True
        return False
    
    async def complete_order(self, order_id: int, worker_id: int) -> bool:
        order = await self.order_repo.get_by_id(order_id)
        if order and order.worker_id == worker_id and order.status in ["accepted", "in_progress"]:
            await self.order_repo.update_status(order_id, "done")
            await self.worker_repo.update_status(worker_id, "free")
            await self.worker_repo.increment_orders(worker_id, True)
            await self._notify_user_completed(order.user_id, order_id)
            await self._assign_pending_order(worker_id)
            return True
        return False
    
    async def reject_order(self, order_id: int, worker_id: int) -> bool:
        order = await self.order_repo.get_by_id(order_id)
        if not order or order.worker_id != worker_id:
            return False
        
        await self.worker_repo.update_status(worker_id, "free")
        worker = await self.worker_repo.get_free_worker()
        
        if worker:
            await self.order_repo.update_status(order_id, "accepted", worker.id)
            await self.worker_repo.update_status(worker.id, "busy")
            await self.worker_repo.increment_orders(worker.id)
            await self._notify_worker(worker.id, order)
            asyncio.create_task(self._order_timeout(order.id, worker.id))
        else:
            await self.order_repo.update_status(order_id, "new", None)
        return True
    
    async def _notify_worker(self, worker_id: int, order: Order):
        try:
            await self.bot.send_message(
                worker_id,
                f"📦 <b>Yangi zakaz #{order.id}</b>\n\n"
                f"👤 Mijoz: <code>{order.user_id}</code>\n"
                f"📝 Matn: {order.text}\n\n"
                f"⏰ 2 daqiqa ichida javob bering!",
                reply_markup=order_worker_keyboard(order.id),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify worker: {e}")
    
    async def _notify_user_completed(self, user_id: int, order_id: int):
        try:
            await self.bot.send_message(
                user_id,
                f"✅ <b>Zakaz #{order_id} bajarildi!</b>\n\nIltimos, labochiga baho bering:",
                reply_markup=rating_keyboard(order_id),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")
    
    async def _assign_pending_order(self, worker_id: int):
        pending = await self.order_repo.get_pending_orders()
        if pending:
            order = pending[0]
            await self.order_repo.update_status(order.id, "accepted", worker_id)
            await self.worker_repo.update_status(worker_id, "busy")
            await self.worker_repo.increment_orders(worker_id)
            await self._notify_worker(worker_id, order)
            asyncio.create_task(self._order_timeout(order.id, worker_id))
    
    async def _order_timeout(self, order_id: int, worker_id: int):
        await asyncio.sleep(settings.ORDER_ACCEPT_TIMEOUT)
        async with async_session_maker() as session:
            order_repo = OrderRepository(session)
            order = await order_repo.get_by_id(order_id)
            if order and order.status == "accepted":
                logger.info(f"Order {order_id} timeout - reassigning")
                service = OrderService(session, self.bot)
                await service.reject_order(order_id, worker_id)


class RatingService:
    def __init__(self, session: AsyncSession):
        self.rating_repo = RatingRepository(session)
        self.worker_repo = WorkerRepository(session)
        self.order_repo = OrderRepository(session)
    
    async def add_rating(self, order_id: int, user_id: int, score: int, comment: Optional[str] = None) -> bool:
        order = await self.order_repo.get_by_id(order_id)
        if not order or order.status != "done" or order.user_id != user_id:
            return False
        if await self.rating_repo.exists_for_order(order_id):
            return False
        
        await self.rating_repo.create(order_id, order.worker_id, user_id, score, comment)
        await self.worker_repo.update_rating(order.worker_id)
        return True


# =============================================================================
# MIDDLEWARE
# =============================================================================

class DatabaseMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        async with async_session_maker() as session:
            data["session"] = session
            return await handler(event, data)


# =============================================================================
# DECORATORS
# =============================================================================

def admin_only(func):
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != settings.ADMIN_ID:
            await message.answer("⛔ Bu buyruq faqat admin uchun!")
            return
        return await func(message, *args, **kwargs)
    return wrapper


# =============================================================================
# HANDLERS
# =============================================================================

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    worker_repo = WorkerRepository(session)
    is_worker = await worker_repo.exists(message.from_user.id)
    
    keyboard = get_main_keyboard(message.from_user.id, is_worker)
    role = "Admin" if message.from_user.id == settings.ADMIN_ID else ("Labochi" if is_worker else "Foydalanuvchi")
    
    await message.answer(
        f"👋 Assalomu alaykum!\n\nSizning rolingiz: <b>{role}</b>\nID: <code>{message.from_user.id}</code>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.message(F.text == "❌ Bekor qilish")
async def cancel_handler(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    worker_repo = WorkerRepository(session)
    is_worker = await worker_repo.exists(message.from_user.id)
    await message.answer("❌ Bekor qilindi", reply_markup=get_main_keyboard(message.from_user.id, is_worker))


# USER HANDLERS
@router.message(F.text == "📦 Zakaz berish")
async def create_order_start(message: Message, state: FSMContext):
    await message.answer("📝 Zakaz matnini yuboring masalan:\nTel raqam:911234567\nIsm:Asadbek\nYuk nomi va og'irligi\nManzil:Toshloqdan Marg'ilonga", reply_markup=cancel_keyboard())
    await state.set_state(UserStates.waiting_order_text)


@router.message(UserStates.waiting_order_text)
async def create_order_finish(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    service = OrderService(session, bot)
    order, worker_id = await service.create_order(message.from_user.id, message.text)
    await state.clear()
    
    worker_repo = WorkerRepository(session)
    is_worker = await worker_repo.exists(message.from_user.id)
    
    if worker_id:
        await message.answer(
            f"✅ Zakaz #{order.id} yaratildi va labochiga yuborildi!",
            reply_markup=get_main_keyboard(message.from_user.id, is_worker)
        )
    else:
        await message.answer(
            f"✅ Zakaz #{order.id} yaratildi!\n⏳ Labochi bo'shashini kutmoqda...",
            reply_markup=get_main_keyboard(message.from_user.id, is_worker)
        )


@router.message(F.text == "📊 Mening zakazlarim")
async def my_orders(message: Message, session: AsyncSession):
    order_repo = OrderRepository(session)
    orders = await order_repo.get_user_orders(message.from_user.id)
    
    if not orders:
        await message.answer("Sizda hali zakazlar yo'q")
        return
    
    text = "📋 <b>Sizning zakazlaringiz:</b>\n\n"
    for order in orders:
        status_emoji = {"new": "🆕", "accepted": "✅", "in_progress": "⚙️", "done": "✔️", "cancelled": "❌"}.get(order.status, "❓")
        status_text = {"new": "Kutilmoqda", "accepted": "Qabul qilindi", "in_progress": "Jarayonda", "done": "Bajarildi", "cancelled": "Bekor qilindi"}.get(order.status, "Noma'lum")
        text += f"{status_emoji} <b>Zakaz #{order.id}</b>\nStatus: {status_text}\nSana: {order.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    
    await message.answer(text, parse_mode="HTML")


# ADMIN HANDLERS
@router.message(F.text == "👷 Labochilar")
@admin_only
async def list_workers(message: Message, session: AsyncSession):
    worker_repo = WorkerRepository(session)
    workers = await worker_repo.get_all()
    
    if not workers:
        await message.answer("Hozircha labochilar yo'q")
        return
    
    text = "👷 <b>Labochilar ro'yxati:</b>\n\n"
    for w in workers:
        pro = "⭐ PRO" if w.is_pro else ""
        status = "🟢 Bo'sh" if w.status == "free" else "🔴 Band"
        text += f"ID: <code>{w.id}</code> {pro}\nStatus: {status}\nReyting: ⭐ {w.rating:.1f}\nZakazlar: {w.completed_orders}/{w.total_orders}\n\n"
    
    await message.answer(text, parse_mode="HTML")


@router.message(F.text == "➕ Labochi qo'shish")
@admin_only
async def add_worker_start(message: Message, state: FSMContext):
    await message.answer("👤 Labochi ID sini yuboring:", reply_markup=cancel_keyboard())
    await state.set_state(AdminStates.waiting_worker_id)


@router.message(AdminStates.waiting_worker_id)
async def add_worker_finish(message: Message, state: FSMContext, session: AsyncSession):
    try:
        worker_id = int(message.text)
    except ValueError:
        await message.answer("❌ Noto'g'ri format! Raqam yuboring.", reply_markup=cancel_keyboard())
        return
    
    worker_repo = WorkerRepository(session)
    if await worker_repo.exists(worker_id):
        await message.answer(f"⚠️ Labochi {worker_id} allaqachon ro'yxatda!", reply_markup=get_main_keyboard(message.from_user.id, False))
    else:
        await worker_repo.create(worker_id)
        await message.answer(f"✅ Labochi {worker_id} qo'shildi!", reply_markup=get_main_keyboard(message.from_user.id, False))
    await state.clear()


@router.message(F.text == "➖ Labochi o'chirish")
@admin_only
async def remove_worker_start(message: Message, state: FSMContext):
    await message.answer("👤 O'chirish uchun labochi ID sini yuboring:", reply_markup=cancel_keyboard())
    await state.set_state(AdminStates.waiting_worker_remove)


@router.message(AdminStates.waiting_worker_remove)
async def remove_worker_finish(message: Message, state: FSMContext, session: AsyncSession):
    try:
        worker_id = int(message.text)
    except ValueError:
        await message.answer("❌ Noto'g'ri format!", reply_markup=cancel_keyboard())
        return
    
    worker_repo = WorkerRepository(session)
    if await worker_repo.delete(worker_id):
        await message.answer(f"✅ Labochi {worker_id} o'chirildi!", reply_markup=get_main_keyboard(message.from_user.id, False))
    else:
        await message.answer(f"❌ Labochi {worker_id} topilmadi!", reply_markup=get_main_keyboard(message.from_user.id, False))
    await state.clear()


@router.message(F.text == "⭐ Pro berish/olish")
@admin_only
async def toggle_pro_start(message: Message, state: FSMContext):
    await message.answer("👤 Pro status o'zgartirish uchun ID:", reply_markup=cancel_keyboard())
    await state.set_state(AdminStates.waiting_pro_toggle)


@router.message(AdminStates.waiting_pro_toggle)
async def toggle_pro_finish(message: Message, state: FSMContext, session: AsyncSession):
    try:
        worker_id = int(message.text)
    except ValueError:
        await message.answer("❌ Noto'g'ri format!", reply_markup=cancel_keyboard())
        return
    
    worker_repo = WorkerRepository(session)
    result = await worker_repo.toggle_pro(worker_id)
    if result is not None:
        status = "⭐ PRO" if result else "Oddiy"
        await message.answer(f"✅ Labochi {worker_id} endi {status}", reply_markup=get_main_keyboard(message.from_user.id, False))
    else:
        await message.answer(f"❌ Labochi topilmadi!", reply_markup=get_main_keyboard(message.from_user.id, False))
    await state.clear()


@router.message(F.text == "📊 Statistika")
@admin_only
async def show_statistics(message: Message, session: AsyncSession):
    order_repo = OrderRepository(session)
    worker_repo = WorkerRepository(session)
    
    stats = await order_repo.get_statistics()
    workers = await worker_repo.get_all()
    
    free = sum(1 for w in workers if w.status == "free")
    busy = sum(1 for w in workers if w.status == "busy")
    pro = sum(1 for w in workers if w.is_pro)
    
    text = (
        f"📊 <b>Statistika</b>\n\n"
        f"<b>Zakazlar:</b>\n"
        f"• Jami: {stats['total']}\n"
        f"• Bajarilgan: {stats['completed']}\n"
        f"• Bekor qilingan: {stats['cancelled']}\n"
        f"• Jarayonda: {stats['in_progress']}\n\n"
        f"<b>Labochilar:</b>\n"
        f"• Jami: {len(workers)}\n"
        f"• Bo'sh: {free}\n"
        f"• Band: {busy}\n"
        f"• Pro: {pro}"
    )
    await message.answer(text, parse_mode="HTML")


# WORKER HANDLERS
@router.message(F.text == "📊 Mening statusim")
async def worker_status(message: Message, session: AsyncSession):
    worker_repo = WorkerRepository(session)
    worker = await worker_repo.get_by_id(message.from_user.id)
    
    if not worker:
        await message.answer("Siz labochi emassiz!")
        return
    
    pro = "⭐ PRO" if worker.is_pro else "Oddiy"
    status = "🟢 Bo'sh" if worker.status == "free" else "🔴 Band"
    
    text = (
        f"👤 <b>Ma'lumotlaringiz:</b>\n\n"
        f"Tur: {pro}\n"
        f"Holat: {status}\n"
        f"Reyting: ⭐ {worker.rating:.1f}\n"
        f"Jami zakazlar: {worker.total_orders}\n"
        f"Bajarilgan: {worker.completed_orders}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text == "📈 Statistika")
async def worker_statistics(message: Message, session: AsyncSession):
    worker_repo = WorkerRepository(session)
    worker = await worker_repo.get_by_id(message.from_user.id)
    
    if not worker:
        await message.answer("Siz labochi emassiz!")
        return
    
    completion = (worker.completed_orders / worker.total_orders * 100) if worker.total_orders > 0 else 0
    
    text = (
        f"📈 <b>Statistikangiz:</b>\n\n"
        f"Reyting: ⭐ {worker.rating:.1f}\n"
        f"Jami: {worker.total_orders}\n"
        f"Bajarilgan: {worker.completed_orders}\n"
        f"Foiz: {completion:.1f}%"
    )
    await message.answer(text, parse_mode="HTML")


# CALLBACK HANDLERS
@router.callback_query(F.data.startswith("accept:"))
async def accept_order(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    order_id = int(callback.data.split(":")[1])
    service = OrderService(session, bot)
    
    if await service.accept_order(order_id, callback.from_user.id):
        await callback.message.edit_text(
            f"✅ <b>Zakaz #{order_id} qabul qilindi!</b>\n\nEndi bajarishingiz mumkin.",
            reply_markup=order_progress_keyboard(order_id),
            parse_mode="HTML"
        )
        await callback.answer("✅ Qabul qilindi!")
    else:
        await callback.answer("❌ Xatolik!", show_alert=True)


@router.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    order_id = int(callback.data.split(":")[1])
    service = OrderService(session, bot)
    
    if await service.reject_order(order_id, callback.from_user.id):
        await callback.message.edit_text(f"❌ Zakaz #{order_id} rad etildi", parse_mode="HTML")
        await callback.answer("✅ Rad etildi")
    else:
        await callback.answer("❌ Xatolik!", show_alert=True)


@router.callback_query(F.data.startswith("complete:"))
async def complete_order(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    order_id = int(callback.data.split(":")[1])
    service = OrderService(session, bot)
    
    if await service.complete_order(order_id, callback.from_user.id):
        await callback.message.edit_text(f"✔️ Zakaz #{order_id} bajarildi!", parse_mode="HTML")
        await callback.answer("✅ Bajarildi!")
    else:
        await callback.answer("❌ Xatolik!", show_alert=True)


@router.callback_query(F.data.startswith("rate:"))
async def rate_order(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    parts = callback.data.split(":")
    order_id, action = int(parts[1]), parts[2]
    
    if action == "skip":
        await callback.message.edit_text(f"Zakaz #{order_id} uchun keyinroq baho bering.")
        await callback.answer("✅ OK")
        return
    
    score = int(action)
    service = RatingService(session)
    
    if await service.add_rating(order_id, callback.from_user.id, score):
        stars = "⭐" * score
        await callback.message.edit_text(f"✅ Rahmat! {stars} baho berildi!\n\nIzoh qoldirmoqchimisiz?")
        await callback.answer(f"✅ {stars}")
        await state.update_data(order_id=order_id, score=score)
        await state.set_state(UserStates.waiting_rating_comment)
        await callback.message.answer("💬 Izoh yuboring yoki '❌ Bekor qilish':", reply_markup=cancel_keyboard())
    else:
        await callback.answer("❌ Baho berib bo'linmadi!", show_alert=True)


@router.message(UserStates.waiting_rating_comment)
async def rating_comment(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    order_id = data.get("order_id")
    
    rating_repo = RatingRepository(session)
    rating = await rating_repo.get_by_order(order_id)
    if rating:
        rating.comment = message.text
        await session.commit()
    
    worker_repo = WorkerRepository(session)
    is_worker = await worker_repo.exists(message.from_user.id)
    
    await message.answer("✅ Rahmat! Fikringiz saqlandi.", reply_markup=get_main_keyboard(message.from_user.id, is_worker))
    await state.clear()


# Handle buttons during state
@router.message(StateFilter("*"), F.text.in_([
    "📦 Zakaz berish", "📊 Mening zakazlarim", "👷 Labochilar", 
    "➕ Labochi qo'shish", "➖ Labochi o'chirish", "⭐ Pro berish/olish",
    "📊 Statistika", "📊 Mening statusim", "📈 Statistika"
]))
async def handle_button_in_state(message: Message, state: FSMContext):
    await state.clear()


# =============================================================================
# MAIN
# =============================================================================

async def main():
    """Main function"""
    # Initialize database
    await init_db()
    
    # Initialize bot
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    
    # Register middleware
    dp.message.middleware(DatabaseMiddleware())
    dp.callback_query.middleware(DatabaseMiddleware())
    
    # Register router
    dp.include_router(router)
    
    logger.info("Bot started!")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())