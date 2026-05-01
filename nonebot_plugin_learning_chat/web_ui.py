import datetime
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, Request, Form, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from jose import jwt
from nonebot import get_app, get_adapter, get_driver, logger
from nonebot.adapters.onebot.v11 import Adapter
from pydantic import BaseModel

try:
    import jieba_fast as jieba
except ImportError:
    import jieba

from .handler import LearningChat
from .models import ChatMessage, ChatContext, ChatAnswer, ChatBlackList
from .config import config_manager, NICKNAME

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["datetime"] = lambda ts: datetime.datetime.fromtimestamp(
    int(ts)
).strftime("%Y-%m-%d %H:%M:%S")

driver = get_driver()


def create_token(username: str) -> str:
    return jwt.encode(
        {
            "username": username,
            "exp": datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=30),
        },
        config_manager.config.web_secret_key,
        algorithm="HS256",
    )


def verify_cookie(request: Request) -> Optional[str]:
    token = request.cookies.get("token")
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, config_manager.config.web_secret_key, algorithms=["HS256"]
        )
        return payload.get("username")
    except Exception:
        return None


def verify_header(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, config_manager.config.web_secret_key, algorithms=["HS256"]
        )
        return payload.get("username")
    except Exception:
        return None


class UserModel(BaseModel):
    username: str
    password: str


async def _get_bot():
    try:
        bots = get_adapter(Adapter).bots
        if len(bots) == 0:
            return None
        return list(bots.values())[0]
    except Exception:
        return None


def _paginate(request: Request, total: int, per_page: int = 10):
    page = int(request.query_params.get("page", 1))
    total_pages = max(1, (total + per_page - 1) // per_page)
    return page, per_page, total_pages


@driver.on_startup
async def init_web():
    if not config_manager.config.enable_web:
        return
    app: FastAPI = get_app()

    try:
        app.mount(
            "/learning_chat/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="learning_chat_static",
        )
    except RuntimeError:
        pass

    # ====== Login ======

    @app.get("/learning_chat/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            "login.html", {"request": request}
        )

    @app.post("/learning_chat/login", response_class=HTMLResponse)
    async def login_post(request: Request):
        try:
            form = await request.form()
            username = form.get("username")
            password = form.get("password")
            if (
                username != config_manager.config.web_username
                or password != config_manager.config.web_password
            ):
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, "error": "用户名或密码错误"},
                    status_code=401,
                )
            token = create_token(username or "")
            response = RedirectResponse("/learning_chat/admin", status_code=302)
            response.set_cookie("token", token, max_age=1800, httponly=True)
            return response
        except Exception as e:
            logger.opt(colors=True).error(f"<r>Login failed: {e}</r>")
            logger.opt(exception=True).error("Login error traceback")
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": f"服务器错误: {e}"},
                status_code=500,
            )

    @app.get("/learning_chat/logout")
    async def logout():
        response = RedirectResponse("/learning_chat/login", status_code=302)
        response.delete_cookie("token")
        return response

    # ====== Admin Pages ======

    @app.get("/learning_chat", response_class=RedirectResponse)
    async def redirect_page():
        return RedirectResponse("/learning_chat/login", status_code=302)

    @app.get("/learning_chat/admin", response_class=HTMLResponse)
    async def admin_config(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)

        group_list = []
        try:
            bot = await _get_bot()
            if bot:
                groups = await bot.get_group_list()
                group_list = [
                    {
                        "label": f'{g["group_name"]}({g["group_id"]})',
                        "value": g["group_id"],
                    }
                    for g in groups
                ]
        except Exception:
            pass

        global_cfg = config_manager.config.model_dump(exclude={"group_config"})
        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "active": "config",
                "global": global_cfg,
                "group_list": group_list,
                "group_id": None,
                "group_config": None,
                "success": request.query_params.get("success"),
            },
        )

    @app.get("/learning_chat/admin/config", response_class=HTMLResponse)
    async def admin_config_detail(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        group_id_str = request.query_params.get("group_id")

        group_list = []
        try:
            bot = await _get_bot()
            if bot:
                groups = await bot.get_group_list()
                group_list = [
                    {
                        "label": f'{g["group_name"]}({g["group_id"]})',
                        "value": g["group_id"],
                    }
                    for g in groups
                ]
        except Exception:
            pass

        global_cfg = config_manager.config.model_dump(exclude={"group_config"})
        group_config = None
        if group_id_str and group_id_str.isdigit():
            group_id = int(group_id_str)
            gc = config_manager.get_group_config(group_id)
            group_config = gc.model_dump()

        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "active": "config",
                "global": global_cfg,
                "group_list": group_list,
                "group_id": group_id_str,
                "group_config": group_config,
                "success": request.query_params.get("success"),
                "error_msg": request.query_params.get("error"),
            },
        )

    @app.post("/learning_chat/admin/config", response_class=HTMLResponse)
    async def admin_config_save(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        form = await request.form()
        form_type = form.get("form_type")
        group_id_str = form.get("group_id", request.query_params.get("group_id", ""))

        if form_type == "global":
            data = dict(form)
            data.pop("form_type", None)
            data["total_enable"] = data.get("total_enable") == "true"
            data["enable_web"] = data.get("enable_web") == "true"
            data["KEYWORDS_SIZE"] = int(data.get("KEYWORDS_SIZE", 3))
            data["cross_group_threshold"] = int(data.get("cross_group_threshold", 3))
            data["learn_max_count"] = int(data.get("learn_max_count", 6))
            data["ban_words"] = [
                w.strip()
                for w in data.get("ban_words", "").split(",")
                if w.strip()
            ]
            data["ban_users"] = [
                int(u.strip())
                for u in data.get("ban_users", "").split(",")
                if u.strip() and u.strip().isdigit()
            ]
            data["dictionary"] = [
                w.strip()
                for w in data.get("dictionary", "").split(",")
                if w.strip()
            ]
            config_manager.config.update(**data)
            config_manager.save()
            await ChatContext.filter(
                count__gt=config_manager.config.learn_max_count
            ).update(count=config_manager.config.learn_max_count)
            await ChatAnswer.filter(
                count__gt=config_manager.config.learn_max_count
            ).update(count=config_manager.config.learn_max_count)
            jieba.load_userdict(config_manager.config.dictionary)
            return RedirectResponse(
                "/learning_chat/admin/config?success=全局配置保存成功", status_code=302
            )
        elif form_type in ("group", "group_all"):
            data = dict(form)
            data.pop("form_type", None)
            data.pop("group_id", None)

            if not data.get("answer_threshold_weights"):
                return RedirectResponse(
                    "/learning_chat/admin/config?error=回复阈值权重不能为空",
                    status_code=302,
                )

            data["enable"] = data.get("enable") == "true"
            data["speak_enable"] = data.get("speak_enable") == "true"
            data["answer_threshold"] = int(data.get("answer_threshold", 4))
            data["answer_threshold_weights"] = [
                int(w.strip())
                for w in data.get("answer_threshold_weights", "").split(",")
                if w.strip().isdigit()
            ]
            data["repeat_threshold"] = int(data.get("repeat_threshold", 3))
            data["break_probability"] = (
                float(data.get("break_probability", 25)) / 100
            )
            data["speak_threshold"] = int(data.get("speak_threshold", 5))
            data["speak_min_interval"] = int(data.get("speak_min_interval", 300))
            data["speak_continuously_probability"] = (
                float(data.get("speak_continuously_probability", 50)) / 100
            )
            data["speak_continuously_max_len"] = int(
                data.get("speak_continuously_max_len", 3)
            )
            data["speak_poke_probability"] = (
                float(data.get("speak_poke_probability", 50)) / 100
            )
            data["ban_words"] = [
                w.strip()
                for w in data.get("ban_words", "").split(",")
                if w.strip()
            ]
            data["ban_users"] = [
                int(u.strip())
                for u in data.get("ban_users", "").split(",")
                if u.strip() and u.strip().lstrip("-").isdigit()
            ]

            bot = await _get_bot()
            if not bot:
                return RedirectResponse(
                    "/learning_chat/admin/config?error=获取bot失败", status_code=302
                )

            if form_type == "group_all":
                groups = await bot.get_group_list()
                target_groups = groups
                success_msg = "已应用至所有群"
            else:
                target_groups = [{"group_id": int(group_id_str)}]
                success_msg = "分群配置保存成功"

            for group in target_groups:
                gid = int(group["group_id"])
                gc = config_manager.get_group_config(gid)
                gc.update(**data)
                config_manager.config.group_config[gid] = gc
            config_manager.save()

            redirect_url = (
                f"/learning_chat/admin/config?group_id={group_id_str}&success={success_msg}"
                if form_type != "group_all"
                else f"/learning_chat/admin/config?success={success_msg}"
            )
            return RedirectResponse(redirect_url, status_code=302)

        return RedirectResponse("/learning_chat/admin/config", status_code=302)

    # ====== Messages Page ======

    @app.get("/learning_chat/admin/messages", response_class=HTMLResponse)
    async def admin_messages(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        page, per_page, total_pages = _paginate(request)

        order_by = request.query_params.get("orderBy", "time")
        order_dir = request.query_params.get("orderDir", "desc")
        sort = f'{"" if order_dir == "asc" else "-"}{order_by}'

        filter_args = {}
        for k, v in {
            "group_id": request.query_params.get("group_id"),
            "user_id": request.query_params.get("user_id"),
            "raw_message": request.query_params.get("message"),
        }.items():
            if v:
                filter_args[f"{k}__contains"] = v

        total = await ChatMessage.filter(**filter_args).count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        items = (
            await ChatMessage.filter(**filter_args)
            .order_by(sort)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .values()
        )

        params = {
            k: v
            for k, v in request.query_params.items()
            if k not in ("page", "perPage")
        }
        return templates.TemplateResponse(
            "messages.html",
            {
                "request": request,
                "active": "messages",
                "items": items,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "order_by": order_by,
                "order_dir": order_dir,
                "params": params,
                "success": request.query_params.get("success"),
            },
        )

    @app.post("/learning_chat/admin/messages/delete/{item_id}")
    async def delete_message(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatMessage.filter(id=item_id).delete()
        return RedirectResponse(
            "/learning_chat/admin/messages?success=删除成功", status_code=302
        )

    @app.post("/learning_chat/admin/messages/ban/{item_id}")
    async def ban_message(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        data = await ChatMessage.get(id=item_id)
        await LearningChat.add_ban(data)
        return RedirectResponse(
            "/learning_chat/admin/messages?success=禁用成功", status_code=302
        )

    @app.post("/learning_chat/admin/messages/delete-all")
    async def delete_all_messages(request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatMessage.all().delete()
        return RedirectResponse(
            "/learning_chat/admin/messages?success=已删除所有消息记录", status_code=302
        )

    # ====== Contexts Page ======

    @app.get("/learning_chat/admin/contexts", response_class=HTMLResponse)
    async def admin_contexts(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        page, per_page, total_pages = _paginate(request)

        order_by = request.query_params.get("orderBy", "time")
        order_dir = request.query_params.get("orderDir", "desc")
        sort = f'{"" if order_dir == "asc" else "-"}{order_by}'

        filter_args = {}
        kw = request.query_params.get("keywords")
        if kw:
            filter_args["keywords__contains"] = kw

        total = await ChatContext.filter(**filter_args).count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        items = (
            await ChatContext.filter(**filter_args)
            .order_by(sort)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .values()
        )

        params = {
            k: v
            for k, v in request.query_params.items()
            if k not in ("page", "perPage")
        }
        return templates.TemplateResponse(
            "contexts.html",
            {
                "request": request,
                "active": "contexts",
                "items": items,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "order_by": order_by,
                "order_dir": order_dir,
                "params": params,
                "success": request.query_params.get("success"),
            },
        )

    @app.post("/learning_chat/admin/contexts/delete/{item_id}")
    async def delete_context(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        c = await ChatContext.get(id=item_id)
        await ChatAnswer.filter(context=c).delete()
        await c.delete()
        return RedirectResponse(
            "/learning_chat/admin/contexts?success=删除成功", status_code=302
        )

    @app.post("/learning_chat/admin/contexts/ban/{item_id}")
    async def ban_context(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        data = await ChatContext.get(id=item_id)
        await LearningChat.add_ban(data)
        return RedirectResponse(
            "/learning_chat/admin/contexts?success=禁用成功", status_code=302
        )

    @app.post("/learning_chat/admin/contexts/delete-all")
    async def delete_all_contexts(request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatContext.all().delete()
        return RedirectResponse(
            "/learning_chat/admin/contexts?success=已删除所有学习内容", status_code=302
        )

    # ====== Answers Page ======

    @app.get("/learning_chat/admin/answers", response_class=HTMLResponse)
    async def admin_answers(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        page, per_page, total_pages = _paginate(request)

        order_by = request.query_params.get("orderBy", "count")
        order_dir = request.query_params.get("orderDir", "desc")
        sort = f'{"" if order_dir == "asc" else "-"}{order_by}'

        context_id = request.query_params.get("context_id")
        filter_args = {}
        if context_id and context_id.isdigit():
            filter_args["context_id"] = int(context_id)
        kw = request.query_params.get("keywords")
        if kw:
            filter_args["keywords__contains"] = kw

        total = await ChatAnswer.filter(**filter_args).count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        items = list(
            await ChatAnswer.filter(**filter_args)
            .order_by(sort)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .values()
        )
        for item in items:
            if isinstance(item.get("messages"), list):
                item["messages"] = [
                    {"msg": m} if not isinstance(m, dict) else m
                    for m in item["messages"]
                ]

        params = {
            k: v
            for k, v in request.query_params.items()
            if k not in ("page", "perPage")
        }
        return templates.TemplateResponse(
            "answers.html",
            {
                "request": request,
                "active": "answers",
                "items": items,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "order_by": order_by,
                "order_dir": order_dir,
                "params": params,
                "context_id": context_id,
                "success": request.query_params.get("success"),
            },
        )

    @app.post("/learning_chat/admin/answers/delete/{item_id}")
    async def delete_answer(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatAnswer.filter(id=item_id).delete()
        return RedirectResponse(
            "/learning_chat/admin/answers?success=删除成功", status_code=302
        )

    @app.post("/learning_chat/admin/answers/ban/{item_id}")
    async def ban_answer(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        data = await ChatAnswer.get(id=item_id)
        await LearningChat.add_ban(data)
        return RedirectResponse(
            "/learning_chat/admin/answers?success=禁用成功", status_code=302
        )

    @app.post("/learning_chat/admin/answers/delete-all")
    async def delete_all_answers(request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        context_id = request.query_params.get("context_id")
        if context_id and context_id.isdigit():
            await ChatAnswer.filter(context_id=int(context_id)).delete()
        else:
            await ChatAnswer.all().delete()
        return RedirectResponse(
            "/learning_chat/admin/answers?success=已删除所有回复", status_code=302
        )

    # ====== Blacklist Page ======

    @app.get("/learning_chat/admin/blacklist", response_class=HTMLResponse)
    async def admin_blacklist(request: Request):
        if not (user := verify_cookie(request)):
            return RedirectResponse("/learning_chat/login", status_code=302)
        page, per_page, total_pages = _paginate(request)

        kw = request.query_params.get("keywords")
        filter_args = {}
        if kw:
            filter_args["keywords__contains"] = kw

        total = await ChatBlackList.filter(**filter_args).count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        items = (
            await ChatBlackList.filter(**filter_args)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .values()
        )

        params = {
            k: v
            for k, v in request.query_params.items()
            if k not in ("page", "perPage")
        }
        return templates.TemplateResponse(
            "blacklist.html",
            {
                "request": request,
                "active": "blacklist",
                "items": items,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "params": params,
                "success": request.query_params.get("success"),
            },
        )

    @app.post("/learning_chat/admin/blacklist/delete/{item_id}")
    async def delete_blacklist(item_id: int, request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatBlackList.filter(id=item_id).delete()
        return RedirectResponse(
            "/learning_chat/admin/blacklist?success=已取消禁用", status_code=302
        )

    @app.post("/learning_chat/admin/blacklist/delete-all")
    async def delete_all_blacklist(request: Request):
        if not verify_cookie(request):
            return RedirectResponse("/learning_chat/login", status_code=302)
        await ChatBlackList.all().delete()
        return RedirectResponse(
            "/learning_chat/admin/blacklist?success=已取消所有禁用", status_code=302
        )

    # ====== REST API Endpoints (JSON, header auth) ======

    def authentication():
        def inner(token: Optional[str] = Header(None)):
            try:
                payload = jwt.decode(
                    token or "",
                    config_manager.config.web_secret_key,
                    algorithms="HS256",
                )
                if (
                    not (username := payload.get("username"))
                    or username != config_manager.config.web_username
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="登录验证失败或已失效，请重新登录",
                    )
            except (jwt.JWTError, jwt.ExpiredSignatureError, AttributeError):
                raise HTTPException(
                    status_code=400,
                    detail="登录验证失败或已失效，请重新登录",
                )

        return Depends(inner)

    @app.post("/learning_chat/api/login", response_class=JSONResponse)
    async def api_login(user: UserModel):
        if (
            user.username != config_manager.config.web_username
            or user.password != config_manager.config.web_password
        ):
            return {"status": -100, "msg": "登录失败，请确认用户ID和密码无误"}
        token_value = create_token(user.username)
        return {"status": 0, "msg": "登录成功", "data": {"token": token_value}}

    @app.get("/learning_chat/api/get_group_list", response_class=JSONResponse, dependencies=[authentication()])
    async def api_get_group_list():
        try:
            bot = await _get_bot()
            if not bot:
                return {"status": -100, "msg": "获取群列表失败，请确认已连接GOCQ"}
            group_list = await bot.get_group_list()
            group_list = [
                {
                    "label": f'{g["group_name"]}({g["group_id"]})',
                    "value": g["group_id"],
                }
                for g in group_list
            ]
            return {"status": 0, "msg": "ok", "data": {"group_list": group_list}}
        except Exception:
            return {"status": -100, "msg": "获取群列表失败，请确认已连接GOCQ"}

    @app.get(
        "/learning_chat/api/chat_global_config",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_global_config():
        try:
            bot = await _get_bot()
            if not bot:
                return {"status": -100, "msg": "获取失败，请确认已连接GOCQ"}
            groups = await bot.get_group_list()
            member_list = []
            for group in groups:
                members = await bot.get_group_member_list(
                    group_id=group["group_id"]
                )
                member_list.extend(
                    [
                        {
                            "label": f'{m["nickname"] or m["card"]}({m["user_id"]})',
                            "value": m["user_id"],
                        }
                        for m in members
                    ]
                )
            config = config_manager.config.model_dump(exclude={"group_config"})
            config["member_list"] = member_list
            return config
        except Exception:
            return {"status": -100, "msg": "获取失败，请确认已连接GOCQ"}

    @app.post(
        "/learning_chat/api/chat_global_config",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_post_global_config(data: dict):
        config_manager.config.update(**data)
        config_manager.save()
        await ChatContext.filter(
            count__gt=config_manager.config.learn_max_count
        ).update(count=config_manager.config.learn_max_count)
        await ChatAnswer.filter(
            count__gt=config_manager.config.learn_max_count
        ).update(count=config_manager.config.learn_max_count)
        jieba.load_userdict(config_manager.config.dictionary)
        return {"status": 0, "msg": "保存成功"}

    @app.get(
        "/learning_chat/api/chat_group_config",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_group_config(group_id: int):
        try:
            bot = await _get_bot()
            if not bot:
                return {"status": -100, "msg": "获取失败，请确认已连接GOCQ"}
            members = await bot.get_group_member_list(group_id=group_id)
            member_list = [
                {
                    "label": f'{m["nickname"] or m["card"]}({m["user_id"]})',
                    "value": m["user_id"],
                }
                for m in members
            ]
            config = config_manager.get_group_config(group_id).model_dump()
            config["break_probability"] = config["break_probability"] * 100
            config["speak_continuously_probability"] = (
                config["speak_continuously_probability"] * 100
            )
            config["speak_poke_probability"] = (
                config["speak_poke_probability"] * 100
            )
            config["member_list"] = member_list
            return config
        except Exception:
            return {"status": -100, "msg": "获取失败，请确认已连接GOCQ"}

    @app.post(
        "/learning_chat/api/chat_group_config",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_post_group_config(group_id: Union[int, str], data: dict):
        if not data.get("answer_threshold_weights"):
            return {"status": 400, "msg": "回复阈值权重不能为空"}
        data["break_probability"] = data.get("break_probability", 25) / 100
        data["speak_continuously_probability"] = (
            data.get("speak_continuously_probability", 50) / 100
        )
        data["speak_poke_probability"] = (
            data.get("speak_poke_probability", 50) / 100
        )
        bot = await _get_bot()
        if not bot:
            return {"status": -100, "msg": "获取群列表失败，请确认已连接GOCQ"}
        groups = (
            [{"group_id": group_id}]
            if group_id != "all"
            else await bot.get_group_list()
        )
        for group in groups:
            config = config_manager.get_group_config(int(group["group_id"]))
            config.update(**data)
            config_manager.config.group_config[int(group["group_id"])] = config
        config_manager.save()
        return {"status": 0, "msg": "保存成功"}

    @app.get(
        "/learning_chat/api/get_chat_messages",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_messages(
        page: int = 1,
        perPage: int = 10,
        orderBy: str = "time",
        orderDir: str = "desc",
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        message: Optional[str] = None,
    ):
        orderBy = (
            (orderBy or "time")
            if (orderDir or "desc") == "asc"
            else f'-{orderBy or "time"}'
        )
        filter_args = {
            f"{k}__contains": v
            for k, v in {
                "group_id": group_id,
                "user_id": user_id,
                "raw_message": message,
            }.items()
            if v
        }
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "items": await ChatMessage.filter(**filter_args)
                .order_by(orderBy)
                .offset((page - 1) * perPage)
                .limit(perPage)
                .values(),
                "total": await ChatMessage.filter(**filter_args).count(),
            },
        }

    @app.get(
        "/learning_chat/api/get_chat_contexts",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_contexts(
        page: int = 1,
        perPage: int = 10,
        orderBy: str = "time",
        orderDir: str = "desc",
        keywords: Optional[str] = None,
    ):
        orderBy = (
            (orderBy or "time")
            if (orderDir or "desc") == "asc"
            else f'-{orderBy or "time"}'
        )
        filter_args = {"keywords__contains": keywords} if keywords else {}
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "items": await ChatContext.filter(**filter_args)
                .order_by(orderBy)
                .offset((page - 1) * perPage)
                .limit(perPage)
                .values(),
                "total": await ChatContext.filter(**filter_args).count(),
            },
        }

    @app.get(
        "/learning_chat/api/get_chat_answers",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_answers(
        context_id: Optional[int] = None,
        page: int = 1,
        perPage: int = 10,
        orderBy: str = "count",
        orderDir: str = "desc",
        keywords: Optional[str] = None,
    ):
        filter_args = {}
        if context_id:
            filter_args["context_id"] = context_id
        if keywords:
            filter_args["keywords__contains"] = keywords
        orderBy = (
            (orderBy or "count")
            if (orderDir or "desc") == "asc"
            else f'-{orderBy or "count"}'
        )
        items = list(
            await ChatAnswer.filter(**filter_args)
            .order_by(orderBy)
            .offset((page - 1) * perPage)
            .limit(perPage)
            .values()
        )
        for item in items:
            if isinstance(item.get("messages"), list):
                item["messages"] = [
                    {"msg": m} if not isinstance(m, dict) else m
                    for m in item["messages"]
                ]
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "items": items,
                "total": await ChatAnswer.filter(**filter_args).count(),
            },
        }

    @app.get(
        "/learning_chat/api/get_chat_blacklist",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_get_blacklist(
        page: int = 1,
        perPage: int = 10,
        keywords: Optional[str] = None,
        bans: Optional[str] = None,
    ):
        filter_args = {"keywords__contains": keywords} if keywords else {}
        items = (
            await ChatBlackList.filter(**filter_args)
            .offset((page - 1) * perPage)
            .limit(perPage)
            .values()
        )
        for item in items:
            item["bans"] = (
                "全局禁用"
                if item.get("global_ban")
                else str(item.get("ban_group_id", [""])[0])
            )
        if bans:
            items = [i for i in items if bans in str(i.get("bans", ""))]
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "items": items,
                "total": await ChatBlackList.filter(**filter_args).count(),
            },
        }

    @app.delete(
        "/learning_chat/api/delete_chat",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_delete_chat(id: int, type: str):
        try:
            if type == "message":
                await ChatMessage.filter(id=id).delete()
            elif type == "context":
                c = await ChatContext.get(id=id)
                await ChatAnswer.filter(context=c).delete()
                await c.delete()
            elif type == "answer":
                await ChatAnswer.filter(id=id).delete()
            elif type == "blacklist":
                await ChatBlackList.filter(id=id).delete()
            return {"status": 0, "msg": "删除成功"}
        except Exception as e:
            return {"status": 500, "msg": f"删除失败，{e}"}

    @app.put(
        "/learning_chat/api/ban_chat",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_ban_chat(id: int, type: str):
        try:
            if type == "message":
                data = await ChatMessage.get(id=id)
            elif type == "context":
                data = await ChatContext.get(id=id)
            else:
                data = await ChatAnswer.get(id=id)
            await LearningChat.add_ban(data)
            return {"status": 0, "msg": "禁用成功"}
        except Exception as e:
            return {"status": 500, "msg": f"禁用失败: {e}"}

    @app.put(
        "/learning_chat/api/delete_all",
        response_class=JSONResponse,
        dependencies=[authentication()],
    )
    async def api_delete_all(type: str, id: Optional[int] = None):
        try:
            if type == "answer":
                if id:
                    await ChatAnswer.filter(context_id=id).delete()
                else:
                    await ChatAnswer.all().delete()
            elif type == "blacklist":
                await ChatBlackList.all().delete()
            elif type == "context":
                await ChatContext.all().delete()
            elif type == "message":
                await ChatMessage.all().delete()
            return {"status": 0, "msg": "操作成功"}
        except Exception as e:
            return {"status": 500, "msg": f"操作失败，{e}"}
