import json
import logging
import os
import threading
import uuid
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from typing import Literal

from authlib.integrations.starlette_client import OAuth  # pyright: ignore[reportMissingImports]
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.sessions import SessionMiddleware

CADDY_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = CADDY_DIR / "data"
PLAYERS_PATH = DATA_DIR / "players.json"
COURSES_PATH = DATA_DIR / "courses.json"
# Static HI -> expected 9-hole Score Differential; source:
# https://swissgolf.ch/media/9-hole_expected_score_differential_2025_2.pdf
WHS_EXPECTED_NINE_SD_PATH = DATA_DIR / "whs_expected_nine_sd.json"

_PLAYERS_LOCK = threading.Lock()
_COURSES_LOCK = threading.Lock()

LOGGER = logging.getLogger("golf_caddy.auth")
logging.basicConfig(level=logging.INFO)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")
SESSION_COOKIE_SECURE = (
    os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)

app = FastAPI(title="TSR Golf Database")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
    max_age=60 * 60 * 24 * 14,  # two weeks
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )


def _is_google_oauth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and hasattr(oauth, "google"))


def _root_path(request: Request) -> str:
    return request.scope.get("root_path", "")


def _route_with_root_path(request: Request, suffix: str) -> str:
    return f"{_root_path(request)}{suffix}"


def _session_user(request: Request) -> dict:
    raw = request.session.get("user")
    if not isinstance(raw, dict):
        return {}
    email = str(raw.get("email") or "").strip().lower()
    if not email:
        return {}
    return {
        "email": email,
        "name": str(raw.get("name") or "").strip(),
    }


def _is_admin(request: Request) -> bool:
    user = _session_user(request)
    return bool(user and ADMIN_EMAIL and user["email"] == ADMIN_EMAIL)


def _require_admin(request: Request) -> dict:
    user = _session_user(request)
    if not user:
        LOGGER.warning("Write access denied (no session)")
        raise HTTPException(status_code=401, detail="Admin login required.")
    if not ADMIN_EMAIL:
        LOGGER.error("Write access denied (ADMIN_EMAIL not configured)")
        raise HTTPException(status_code=500, detail="Admin email is not configured.")
    if user["email"] != ADMIN_EMAIL:
        LOGGER.warning("Write access denied for email=%s", user["email"])
        raise HTTPException(status_code=403, detail="You are not authorized to modify data.")
    LOGGER.info("Write access granted for email=%s", user["email"])
    return user


def _load_whs_expected_nine_sd() -> dict[str, float]:
    """Swiss Golf 2025 table: HI (tenth) -> expected 9-hole Score Differential."""
    if not WHS_EXPECTED_NINE_SD_PATH.exists():
        return {}
    try:
        raw = json.loads(WHS_EXPECTED_NINE_SD_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def _read_players() -> list[dict]:
    if not PLAYERS_PATH.exists():
        return []
    try:
        data = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict)]


def _write_players(players: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLAYERS_PATH.write_text(
        json.dumps(players, indent=2),
        encoding="utf-8",
    )


def _read_courses() -> list[dict]:
    if not COURSES_PATH.exists():
        return []
    try:
        data = json.loads(COURSES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)]


def _write_courses(courses: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COURSES_PATH.write_text(
        json.dumps(courses, indent=2),
        encoding="utf-8",
    )


def _player_display_name(p: dict) -> str:
    return f"{p.get('firstName', '').strip()} {p.get('lastName', '').strip()}".strip()


class PlayerCreate(BaseModel):
    firstName: str
    lastName: str


DriveOutcome = Literal["Fairway", "Left", "Right", "Long", "Short", "None"]

PenaltyType = Literal["Bunker- Green Side", "Bunker- Fairway", "Hazard or OB"]


class PlayerRoundHoleIn(BaseModel):
    par: int = Field(ge=0, le=15)
    distance: int = Field(ge=0, le=999)
    score: int = Field(ge=1, le=20)
    putts: int = Field(ge=0, le=15)
    drive: DriveOutcome
    gir: bool
    penalties: list[PenaltyType] = Field(default_factory=list)

    @model_validator(mode="after")
    def par3_requires_drive_none(self):
        if self.par == 3 and self.drive != "None":
            raise ValueError("drive must be None for par-3 holes.")
        if self.par != 3 and self.drive == "None":
            raise ValueError('drive may only be "None" on par-3 holes.')
        return self

    @field_validator("penalties", mode="before")
    @classmethod
    def coerce_penalties(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("penalties")
    @classmethod
    def penalties_max_len(cls, v: list) -> list:
        if len(v) > 40:
            raise ValueError("penalties may contain at most 40 entries per hole.")
        return v


class PlayerRoundAppend(BaseModel):
    course: str
    tees: str
    courseId: str
    playedDate: str
    holes: dict[str, PlayerRoundHoleIn]


class CourseCreate(BaseModel):
    name: str
    tee_box: str
    slope: float = Field(ge=55, le=155)
    rating: float


class HoleUpdate(BaseModel):
    hole: int
    par_value: int
    distance: int


class CourseHolesUpdate(BaseModel):
    holes: list[HoleUpdate]


def _default_holes() -> list[dict]:
    return [{"hole": n, "par_value": 0, "distance": 0} for n in range(1, 19)]


def _normalize_course(c: dict) -> dict:
    out = dict(c)
    holes = out.get("holes")
    if not isinstance(holes, list) or len(holes) != 18:
        out["holes"] = _default_holes()
    return out


def _course_sort_key(c: dict) -> tuple[str, str]:
    return (
        (c.get("name") or "").strip().lower(),
        (c.get("tee_box") or "").strip().lower(),
    )


INSIGHT_STUB_TITLES: dict[str, str] = {
    "by-par": "By Par",
    "driving": "Driving",
    "putting": "Putting",
}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _session_user(request)
    root_path = _root_path(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "root_path": root_path,
            "whs_expected_nine_sd": _load_whs_expected_nine_sd(),
            "is_admin": _is_admin(request),
            "viewer_email": user.get("email", ""),
            "oauth_enabled": _is_google_oauth_enabled(),
            "admin_email_configured": bool(ADMIN_EMAIL),
            "auth_login_url": _route_with_root_path(request, "/auth/login"),
            "auth_logout_url": _route_with_root_path(request, "/auth/logout"),
        },
    )


@app.get("/auth/login")
async def auth_login(request: Request):
    if not _is_google_oauth_enabled():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured.")
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, str(redirect_uri))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not _is_google_oauth_enabled():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured.")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"OAuth failed: {exc}")
    user_info = token.get("userinfo") or {}
    if not user_info:
        user_info = await oauth.google.parse_id_token(request, token) or {}
    email = str(user_info.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Google account email is unavailable.")
    request.session["user"] = {
        "email": email,
        "name": str(user_info.get("name") or "").strip(),
    }
    return RedirectResponse(url=_route_with_root_path(request, "/"), status_code=302)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=_route_with_root_path(request, "/"), status_code=302)


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = _session_user(request)
    return {
        "isAdmin": _is_admin(request),
        "email": user.get("email", ""),
        "oauthEnabled": _is_google_oauth_enabled(),
    }


@app.get("/insights/{slug}", response_class=HTMLResponse)
async def insight_stub(request: Request, slug: str):
    title = INSIGHT_STUB_TITLES.get(slug)
    if title is None:
        raise HTTPException(status_code=404, detail="Unknown insights page.")
    root_path = request.scope.get("root_path", "")
    return templates.TemplateResponse(
        request,
        "insight_stub.html",
        {
            "request": request,
            "root_path": root_path,
            "title": title,
            "slug": slug,
        },
    )


@app.get("/api/players")
def list_players():
    with _PLAYERS_LOCK:
        players = _read_players()
    return sorted(players, key=lambda p: _player_display_name(p).lower())


@app.post("/api/players")
def create_player(body: PlayerCreate, _admin: dict = Depends(_require_admin)):
    first = body.firstName.strip()
    last = body.lastName.strip()
    if not first or not last:
        raise HTTPException(
            status_code=422,
            detail="firstName and lastName must be non-empty after trimming.",
        )
    new_player = {
        "id": str(uuid.uuid4()),
        "firstName": first,
        "lastName": last,
    }
    with _PLAYERS_LOCK:
        players = _read_players()
        key = (first.lower(), last.lower())
        for p in players:
            if (
                p.get("firstName", "").strip().lower() == key[0]
                and p.get("lastName", "").strip().lower() == key[1]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A player with that name already exists.",
                )
        players.append(new_player)
        _write_players(players)
    return new_player


@app.post("/api/players/{player_id}/rounds")
def append_player_round(
    player_id: str, body: PlayerRoundAppend, _admin: dict = Depends(_require_admin)
):
    valid_hole_keys = {str(n) for n in range(1, 19)}
    if not body.holes:
        raise HTTPException(
            status_code=422,
            detail="holes must contain at least one hole (1–18).",
        )
    for k in body.holes.keys():
        if k not in valid_hole_keys:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid hole key {k!r}; use string keys 1 through 18 only.",
            )
    played = body.playedDate.strip()
    if not played:
        raise HTTPException(
            status_code=422,
            detail="playedDate is required (YYYY-MM-DD).",
        )
    try:
        date_type.fromisoformat(played)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="playedDate must be a valid calendar date in YYYY-MM-DD format.",
        )
    normalized_holes: dict[str, dict] = {}
    for key in sorted(body.holes.keys(), key=int):
        h = body.holes[key]
        normalized_holes[key] = {
            "par": h.par,
            "distance": h.distance,
            "score": h.score,
            "putts": h.putts,
            "drive": h.drive,
            "gir": h.gir,
            "penalties": list(h.penalties),
        }
    saved_at = datetime.now(timezone.utc).isoformat()
    round_record = {
        "course": body.course.strip(),
        "tees": body.tees.strip(),
        "courseId": body.courseId.strip(),
        "playedDate": played,
        "savedAt": saved_at,
        "holes": normalized_holes,
    }
    with _PLAYERS_LOCK:
        players = _read_players()
        idx = next((i for i, p in enumerate(players) if p.get("id") == player_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Player not found.")
        player = dict(players[idx])
        rounds = player.get("rounds")
        if not isinstance(rounds, list):
            rounds = []
        rounds = rounds + [round_record]
        player["rounds"] = rounds
        players[idx] = player
        _write_players(players)
    return player


@app.get("/api/courses")
def list_courses():
    with _COURSES_LOCK:
        courses = _read_courses()
    normalized = [_normalize_course(c) for c in courses]
    return sorted(normalized, key=_course_sort_key)


@app.post("/api/courses")
def create_course(body: CourseCreate, _admin: dict = Depends(_require_admin)):
    name = body.name.strip()
    tee_box = body.tee_box.strip()
    if not name or not tee_box:
        raise HTTPException(
            status_code=422,
            detail="name and tee_box must be non-empty after trimming.",
        )
    slope = round(float(body.slope), 1)
    rating = float(body.rating)
    if not 20.0 <= rating <= 81.0:
        raise HTTPException(
            status_code=422,
            detail="rating must be between 20.0 and 81.0.",
        )
    new_course = {
        "id": str(uuid.uuid4()),
        "name": name,
        "tee_box": tee_box,
        "slope": slope,
        "rating": round(rating, 1),
        "holes": _default_holes(),
    }
    dup_key = (name.lower(), tee_box.lower())
    with _COURSES_LOCK:
        courses = _read_courses()
        for c in courses:
            c_name = (c.get("name") or "").strip().lower()
            c_tee = (c.get("tee_box") or "").strip().lower()
            if (c_name, c_tee) == dup_key:
                raise HTTPException(
                    status_code=409,
                    detail="A course with this name and tee box already exists.",
                )
        courses.append(new_course)
        _write_courses(courses)
    return _normalize_course(new_course)


@app.put("/api/courses/{course_id}")
def update_course_holes(
    course_id: str, body: CourseHolesUpdate, _admin: dict = Depends(_require_admin)
):
    if len(body.holes) != 18:
        raise HTTPException(
            status_code=422,
            detail="holes must contain exactly 18 entries.",
        )
    by_hole: dict[int, HoleUpdate] = {}
    for h in body.holes:
        if h.hole in by_hole:
            raise HTTPException(
                status_code=422,
                detail="duplicate hole number in payload.",
            )
        by_hole[h.hole] = h
    if set(by_hole.keys()) != set(range(1, 19)):
        raise HTTPException(
            status_code=422,
            detail="holes must include hole numbers 1 through 18 exactly once.",
        )
    new_holes: list[dict] = []
    for n in range(1, 19):
        hu = by_hole[n]
        if not 0 <= hu.par_value <= 15:
            raise HTTPException(
                status_code=422,
                detail=f"par_value for hole {n} must be between 0 and 15.",
            )
        if not 0 <= hu.distance <= 999:
            raise HTTPException(
                status_code=422,
                detail=f"distance for hole {n} must be between 0 and 999.",
            )
        new_holes.append(
            {
                "hole": n,
                "par_value": int(hu.par_value),
                "distance": int(hu.distance),
            }
        )
    with _COURSES_LOCK:
        courses = _read_courses()
        idx = next((i for i, c in enumerate(courses) if c.get("id") == course_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Course not found.")
        course = dict(courses[idx])
        course["holes"] = new_holes
        courses[idx] = course
        _write_courses(courses)
    return _normalize_course(course)


if __name__ == "__main__":
    import uvicorn

    # Run directly against the in-process app object so startup does not depend on cwd.
    uvicorn.run(app, host="127.0.0.1", port=8002, reload=True)
