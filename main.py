"""El Gran Golpe v1.9 corregido — odds-first scanner with Gemini.
Current picks require current odds; API-Football historical trends are a separate command.
This bot never places bets and never invents fixtures or prices.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FOOTBALL_BASE = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io").rstrip("/")
ODDS_API_BASE = os.getenv(
    "ODDS_API_BASE",
    os.getenv("ODDS_API_BASE_URL", "https://api.the-odds-api.com")
).rstrip("/")
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "1572804298").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip().removeprefix("models/")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Six-hour default to protect a limited odds API quota.
POLL_SECONDS = max(300, int(os.getenv("POLL_SECONDS", "21600")))
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.70"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))
MIN_BOOKMAKERS = max(2, int(os.getenv("MIN_BOOKMAKERS", "3")))
MAX_SPORTS_PER_SCAN = max(1, int(os.getenv("MAX_SPORTS_PER_SCAN", "5")))
DB_PATH = os.getenv("DB_PATH", "el_gran_golpe.sqlite3")
REGIONS = os.getenv("ODDS_REGIONS", "eu")
DEFAULT_SPORTS = (
    "soccer_epl,soccer_spain_la_liga,soccer_germany_bundesliga,"
    "soccer_italy_serie_a,soccer_france_ligue_one,soccer_netherlands_eredivisie,"
    "soccer_portugal_primeira_liga,soccer_uefa_champs_league,"
    "soccer_uefa_europa_league,soccer_uefa_europa_conference_league,"
    "soccer_england_efl_champ,soccer_spl,basketball_nba"
)
SPORTS = [s.strip() for s in os.getenv("SPORT_KEYS", DEFAULT_SPORTS).split(",") if s.strip()]
# Standard markets: winner, handicap and totals. Extra soccer markets (BTTS/corners/cards)
# depend on the provider endpoint and subscription; configure only if your API key supports them.
MARKETS = os.getenv("ODDS_MARKETS", "h2h,totals,spreads")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("el-gran-golpe")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS picks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, sport TEXT,
      event_id TEXT, event TEXT, market TEXT, selection TEXT, odds REAL,
      fair_probability REAL, fair_odds REAL, edge REAL, score INTEGER,
      bookmaker TEXT, status TEXT DEFAULT 'PENDING', result_note TEXT,
      UNIQUE(event_id, market, selection, bookmaker, odds)
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ElGranGolpe/1.4"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        detail = body or str(exc.reason)
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message") or parsed.get("error") or detail
        except Exception:
            pass
        if exc.code == 401:
            detail = f"HTTP 401: clave inválida o cuota/plan no disponible. Detalle: {str(detail)[:240]}"
        elif exc.code == 403:
            detail = f"HTTP 403: el plan no permite este deporte/mercado/temporada. Detalle: {str(detail)[:240]}"
        elif exc.code == 429:
            detail = f"HTTP 429: límite de solicitudes alcanzado. Detalle: {str(detail)[:240]}"
        else:
            detail = f"HTTP {exc.code}: {str(detail)[:350]}"
        raise RuntimeError(detail) from None


def telegram(method: str, payload: dict[str, Any]) -> Any:
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Render.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body[:250]}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result.get("result")


def send_message(chat_id: str | int, text: str) -> None:
    telegram("sendMessage", {
        "chat_id": str(chat_id),
        "text": text[:4000],
        "disable_web_page_preview": "true"
    })


def odds_url(sport: str) -> str:
    params = urllib.parse.urlencode({
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "decimal",
    })
    return f"{ODDS_API_BASE}/v4/sports/{urllib.parse.quote(sport)}/odds/?{params}"


def fetch_sport(sport: str) -> list[dict[str, Any]]:
    if not ODDS_API_KEY:
        raise RuntimeError("Falta ODDS_API_KEY en Render.")
    data = http_json(odds_url(sport), headers={"User-Agent": "ElGranGolpe/1.4"})
    if isinstance(data, dict):
        if data.get("message") and not data.get("data"):
            raise RuntimeError(str(data["message"]))
        data = data.get("data", [])
    if not isinstance(data, list):
        raise ValueError(f"Respuesta inesperada de API para {sport}")
    return data


def odds_error_stops_scan(error: Exception) -> bool:
    """Do not waste remaining requests after invalid key/quota/rate-limit errors."""
    message = str(error).casefold()
    return any(token in message for token in (
        "http 401", "unauthorized", "quota", "credits exhausted", "http 429", "rate limit"
    ))


# API-Football league IDs. The year/season availability depends on the competition.
FOOTBALL_LEAGUES = {
    "Premier League": 39, "La Liga": 140, "Bundesliga": 78, "Serie A": 135,
    "Ligue 1": 61, "Eredivisie": 88, "Primeira Liga": 94,
    "Champions League": 2, "Europa League": 3, "Conference League": 848,
    "Championship": 40, "Scottish Premiership": 179,
}

def football_get(endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if not API_FOOTBALL_KEY:
        raise RuntimeError("Falta API_FOOTBALL_KEY en Render.")
    query = urllib.parse.urlencode(params)
    url = f"{API_FOOTBALL_BASE}/{endpoint.lstrip('/')}?{query}"
    data = http_json(url, headers={"x-apisports-key": API_FOOTBALL_KEY, "User-Agent": "ElGranGolpe/1.6"})
    if not isinstance(data, dict):
        raise RuntimeError("Respuesta inesperada de API-Football.")
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API-Football: {str(errors)[:220]}")
    response = data.get("response", [])
    if not isinstance(response, list):
        raise RuntimeError("API-Football no devolvió una lista de resultados.")
    return response

def football_stats_scan(chat_id: str | int) -> tuple[int, int, list[str]]:
    """Statistical screening from recent league results; deliberately does not invent bookmaker odds."""
    try:
        season = int(os.getenv("FOOTBALL_SEASON", str(datetime.now().year)))
    except ValueError:
        raise RuntimeError("FOOTBALL_SEASON debe ser un año numérico, por ejemplo 2026.")
    if season < 2000 or season > datetime.now().year + 1:
        raise RuntimeError("FOOTBALL_SEASON no parece un año válido.")
    leagues = list(FOOTBALL_LEAGUES.items())[:MAX_SPORTS_PER_SCAN]
    errors: list[str] = []
    signals: list[str] = []
    checked = 0
    for league_name, league_id in leagues:
        try:
            recent = football_get("fixtures", {"league": league_id, "season": season, "last": 20})
            upcoming = football_get("fixtures", {"league": league_id, "season": season, "next": 10})
            team_stats: dict[int, dict[str, float]] = {}
            for item in recent:
                fixture = item.get("fixture", {})
                if fixture.get("status", {}).get("short") not in {"FT", "AET", "PEN"}:
                    continue
                teams = item.get("teams", {})
                goals = item.get("goals", {})
                home, away = teams.get("home", {}), teams.get("away", {})
                hg, ag = goals.get("home"), goals.get("away")
                if hg is None or ag is None:
                    continue
                for team, scored, conceded in ((home, hg, ag), (away, ag, hg)):
                    tid = team.get("id")
                    if tid is None: continue
                    st = team_stats.setdefault(int(tid), {"n": 0, "gf": 0, "ga": 0, "scored": 0, "conceded": 0, "btts": 0, "over25": 0})
                    st["n"] += 1; st["gf"] += scored; st["ga"] += conceded
                    st["scored"] += int(scored > 0); st["conceded"] += int(conceded > 0)
                    st["btts"] += int(scored > 0 and conceded > 0); st["over25"] += int(scored + conceded > 2)
            for item in upcoming:
                fixture = item.get("fixture", {})
                if fixture.get("status", {}).get("short") not in {"NS", "TBD"}: continue
                teams = item.get("teams", {})
                home, away = teams.get("home", {}), teams.get("away", {})
                hs, aws = team_stats.get(int(home.get("id", -1))), team_stats.get(int(away.get("id", -1)))
                if not hs or not aws or hs["n"] < 3 or aws["n"] < 3: continue
                checked += 1
                hname, aname = home.get("name", "Local"), away.get("name", "Visitante")
                btts_rate = ((hs["scored"] / hs["n"]) + (aws["scored"] / aws["n"]) + (hs["conceded"] / hs["n"]) + (aws["conceded"] / aws["n"])) / 4
                over_rate = (hs["over25"] / hs["n"] + aws["over25"] / aws["n"]) / 2
                if btts_rate >= 0.68:
                    signals.append(f"⚽ {league_name}: {hname} vs {aname}\n📊 Ambos marcan: tendencia estadística alta ({btts_rate:.0%} indicador combinado).")
                if over_rate >= 0.65:
                    signals.append(f"📈 {league_name}: {hname} vs {aname}\nOver 2.5 goles: tendencia histórica ({over_rate:.0%} promedio de frecuencia de ambos equipos).")
        except Exception as exc:
            log.warning("API-Football fallo en %s: %s", league_name, exc)
            errors.append(f"{league_name}: {str(exc)[:130]}")
    for signal in signals[:15]:
        send_message(chat_id, "📊 EL GRAN GOLPE · SEÑAL ESTADÍSTICA\n" + signal + "\n⚠️ No incluye cuotas reales ni garantiza resultados.")
    summary = (f"🔎 Análisis estadístico API-Football terminado.\nTemporada solicitada: {season}\nLigas consultadas: {len(leagues)}\nPartidos próximos evaluados: {checked}\nSeñales estadísticas: {len(signals)}\nErrores de liga: {len(errors)}\n\nNo consulta cuotas. Si tu plan no admite la temporada actual, el resultado no sirve para pronosticar partidos actuales; no se sustituyó por temporadas antiguas.")
    send_message(chat_id, summary)
    return len(signals), checked, errors

def normalize_probs(outcomes: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    valid = []
    for outcome in outcomes:
        try:
            price = float(outcome.get("price"))
            if price > 1:
                valid.append((outcome, 1 / price))
        except (TypeError, ValueError):
            continue
    total = sum(prob for _, prob in valid)
    if total <= 0:
        return {}
    return {
        (str(outcome.get("name", "")), str(outcome.get("point", ""))): prob / total
        for outcome, prob in valid
    }


def score_edge(edge: float) -> int:
    return max(1, min(10, int(round(5 + edge * 100))))


def analyze_event(sport: str, event: dict[str, Any]) -> list[dict[str, Any]]:
    pools: dict[tuple[str, str, str], list[float]] = {}
    offers = []
    for book in event.get("bookmakers", []) or []:
        book_name = str(book.get("title") or book.get("key") or "bookmaker")
        for market in book.get("markets", []) or []:
            market_key = str(market.get("key", ""))
            outcomes = market.get("outcomes", []) or []
            probs = normalize_probs(outcomes)
            for outcome in outcomes:
                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                name = str(outcome.get("name", ""))
                point = str(outcome.get("point", ""))
                key = (market_key, name, point)
                if key in probs:
                    pools.setdefault(key, []).append(probs[key])
                    offers.append({"key": key, "price": price, "book": book_name})
    results = []
    for offer in offers:
        key = offer["key"]
        probs = pools.get(key, [])
        if len(probs) < MIN_BOOKMAKERS:
            continue
        fair_p = mean(probs)
        if fair_p <= 0:
            continue
        fair_odds = 1 / fair_p
        edge = offer["price"] * fair_p - 1
        if offer["price"] < MIN_ODDS or edge < MIN_EDGE:
            continue
        market_key, name, point = key
        selection = name + (f" {point}" if point else "")
        event_name = f"{event.get('home_team', '?')} vs {event.get('away_team', '?')}"
        results.append({
            "sport": sport,
            "event_id": str(event.get("id", event_name)),
            "event": event_name,
            "market": market_key,
            "selection": selection,
            "odds": offer["price"],
            "fair_probability": fair_p,
            "fair_odds": fair_odds,
            "edge": edge,
            "score": score_edge(edge),
            "bookmaker": offer["book"],
            "commence_time": event.get("commence_time", "")
        })
    return results


def save_new_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conn = db()
    fresh = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for pick in sorted(picks, key=lambda item: item["edge"], reverse=True):
            cur = conn.execute("""INSERT OR IGNORE INTO picks
              (created_at,sport,event_id,event,market,selection,odds,fair_probability,
               fair_odds,edge,score,bookmaker)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (now, pick["sport"], pick["event_id"], pick["event"], pick["market"],
               pick["selection"], pick["odds"], pick["fair_probability"],
               pick["fair_odds"], pick["edge"], pick["score"], pick["bookmaker"]))
            if cur.rowcount:
                pick["pick_id"] = cur.lastrowid
                fresh.append(pick)
        conn.commit()
    finally:
        conn.close()
    return fresh


def format_pick(pick: dict[str, Any]) -> str:
    kickoff = pick.get("commence_time") or "hora por confirmar"
    return (
        f"🚨 EL GRAN GOLPE | SEÑAL #{pick.get('pick_id', '—')}\n"
        f"🏟️ {pick['event']}\n⚽ Deporte/torneo: {pick['sport']}\n"
        f"🎯 Mercado: {pick['market']} — {pick['selection']}\n"
        f"📈 Cuota: {pick['odds']:.2f} | Cuota justa estimada: {pick['fair_odds']:.2f}\n"
        f"🧮 Prob. consenso sin margen: {pick['fair_probability']*100:.1f}%\n"
        f"💹 Diferencia estimada vs cuota: {pick['edge']*100:+.1f}%\n"
        f"🔟 Índice de señal: {pick['score']}/10 (heurístico, no probabilidad de ganar)\n"
        f"🏷️ Casa detectada: {pick['bookmaker']}\n🕒 Inicio: {kickoff}\n"
        "⚠️ Señal basada en consenso de cuotas; verifica la cuota antes de apostar."
    )


def scan(chat_id: str | int | None = None, report_summary: bool = True) -> tuple[int, int, list[str]]:
    target = str(chat_id or CHAT_ID)
    if not target:
        raise RuntimeError("Configura TELEGRAM_CHAT_ID o envía /start al bot.")
    all_picks: list[dict[str, Any]] = []
    errors = []
    sports_to_scan = SPORTS[:MAX_SPORTS_PER_SCAN]
    for sport in sports_to_scan:
        try:
            events = fetch_sport(sport)
            for event in events:
                all_picks.extend(analyze_event(sport, event))
        except Exception as exc:
            log.warning("Fallo en %s: %s", sport, exc)
            errors.append(f"{sport}: {str(exc)[:160]}")
            if odds_error_stops_scan(exc):
                log.error("Se detiene el barrido para no gastar más solicitudes: %s", exc)
                break
    fresh = save_new_picks(all_picks)
    for pick in fresh[:20]:
        send_message(target, format_pick(pick))
    summary = (
        f"🔎 Barrido terminado.\nDeportes consultados: {len(sports_to_scan)}\n"
        f"Señales nuevas: {len(fresh)}\n"
        f"Candidatos detectados (incluye repetidos): {len(all_picks)}\n"
        f"Cuota mínima: {MIN_ODDS:.2f} | Diferencia mínima: {MIN_EDGE*100:.1f}%\n"
        f"Proveedores con error: {len(errors)}\n"
        "El consenso de cuotas no demuestra por sí solo una ventaja real."
    )
    if report_summary or fresh or errors:
        send_message(target, summary)
    return len(fresh), len(all_picks), errors


def performance() -> str:
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM picks WHERE status='PENDING'").fetchone()[0]
        won = conn.execute("SELECT COUNT(*) FROM picks WHERE status='WON'").fetchone()[0]
        lost = conn.execute("SELECT COUNT(*) FROM picks WHERE status='LOST'").fetchone()[0]
        push = conn.execute("SELECT COUNT(*) FROM picks WHERE status='PUSH'").fetchone()[0]
        settled = won + lost + push
        profit = conn.execute("""SELECT COALESCE(SUM(
          CASE WHEN status='WON' THEN odds-1 WHEN status='LOST' THEN -1 ELSE 0 END),0)
          FROM picks WHERE status IN ('WON','LOST','PUSH')""").fetchone()[0]
        roi = profit / settled * 100 if settled else 0
        return (
            f"📊 REGISTRO EL GRAN GOLPE\nTotal señales: {total}\nPendientes: {pending}\n"
            f"Ganadas: {won} | Perdidas: {lost} | Nulas: {push}\n"
            f"Beneficio simulado: {profit:+.2f} unidades\nROI sobre picks liquidados: {roi:+.1f}%\n"
            "El registro solo es fiable si se liquidan todos los picks."
        )
    finally:
        conn.close()


def settle(args: list[str], chat_id: str | int) -> None:
    if len(args) != 2 or not args[0].isdigit() or args[1].lower() not in {"won", "lost", "push"}:
        send_message(chat_id, "Uso: /settle ID won|lost|push\nEjemplo: /settle 12 won")
        return
    conn = db()
    try:
        cur = conn.execute(
            "UPDATE picks SET status=? WHERE id=? AND status='PENDING'",
            (args[1].upper(), int(args[0]))
        )
        conn.commit()
        send_message(chat_id, "Resultado guardado." if cur.rowcount else "No existe ese pick pendiente.")
    finally:
        conn.close()


def combo(chat_id: str | int) -> None:
    conn = db()
    try:
        rows = conn.execute("""SELECT id,event,market,selection,odds,bookmaker
          FROM picks WHERE status='PENDING' ORDER BY created_at DESC LIMIT 80""").fetchall()
    finally:
        conn.close()
    chosen = []
    seen_events = set()
    for row in rows:
        if row["event"] in seen_events:
            continue
        chosen.append(row)
        seen_events.add(row["event"])
        if len(chosen) == 3:
            break
    if len(chosen) < 2:
        send_message(chat_id, "Aún no hay suficientes señales reales guardadas para una combinada. No inventaré selecciones.")
        return
    total = 1.0
    lines = ["🧩 COMBINADA SUGERIDA (señales guardadas; revisa cuotas)"]
    for row in chosen:
        total *= float(row["odds"])
        lines.append(f"\n#{row['id']} {row['event']}\n{row['market']} — {row['selection']} | cuota {float(row['odds']):.2f}")
    lines.append(f"\n\nCuota combinada aproximada: {total:.2f}\n⚠️ Una combinada tiene más riesgo.")
    send_message(chat_id, "".join(lines))


def get_saved_candidates(limit: int = 60) -> list[dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute("""SELECT id,sport,event,market,selection,odds,
          fair_probability,edge,bookmaker,created_at,status FROM picks
          WHERE status='PENDING' ORDER BY edge DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def is_betting_request(text: str) -> bool:
    words = (
        "pick", "apuesta", "apostar", "cuota", "combinada", "combi", "stake",
        "pronóstico", "pronostico", "partido de hoy", "ganador", "over", "under",
        "ambos marcan", "córners", "corners", "goleador", "nba", "champions",
        "premier", "real madrid", "barcelona", "fútbol", "futbol"
    )
    lowered = text.casefold()
    return any(word in lowered for word in words)


def gemini_reply(user_request: str, chat_id: str | int) -> None:
    """Answer general questions with Gemini; betting picks must use saved API data."""
    if not GEMINI_API_KEY:
        send_message(chat_id, "Gemini no está configurado: falta GEMINI_API_KEY en Render.")
        return

    candidates = get_saved_candidates()
    betting_request = is_betting_request(user_request)

    # Do not block Gemini just because the odds API has no saved picks.
    # For betting requests, explicitly forbid invented odds/picks.
    # Prompt compacto para reducir tokens y evitar que Gemini afirme haber consultado datos no conectados.
    prompt = (
        "Eres El Gran Golpe, asistente de análisis deportivo. Español, breve y directo. "
        "Preguntas generales: explica conceptos sin fingir que consultaste datos actuales. "
        "Para picks/pronósticos/combinadas, usa SOLO los candidatos reales del JSON. "
        "No inventes partidos, cuotas, lesiones ni estadísticas. Si no hay candidatos adecuados, "
        "dilo claramente y no inventes una apuesta. Describe métodos en condicional (revisaría) "
        "salvo que los datos realmente estén disponibles. Nunca garantices ganancias.\n"
        f"Solicitud de apuestas: {'sí' if betting_request else 'no'}. "
        f"Usuario: {user_request[:800]}\n"
        f"Candidatos guardados: {json.dumps(candidates[:20], ensure_ascii=False, separators=(',', ':'))}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(GEMINI_MODEL)}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}"
    )
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 350}
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    data = None
    last_error = "error desconocido"
    started_at = time.monotonic()
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {body[:350]}"
            log.warning("Fallo Gemini intento %s/2: %s", attempt + 1, last_error)
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 1:
                break
        except Exception as exc:
            last_error = str(exc)
            log.warning("Fallo Gemini intento %s/2: %s", attempt + 1, last_error)
            if attempt == 1:
                break
        time.sleep(0.25)

    elapsed = time.monotonic() - started_at
    log.info("Gemini terminó en %.2f s (modelo=%s)", elapsed, GEMINI_MODEL)
    if data is None:
        log.error("Gemini no respondió tras reintentos: %s", last_error)
        send_message(chat_id, f"Gemini no respondió tras 2 intentos ({last_error[:180]}). Comprueba GEMINI_API_KEY y GEMINI_MODEL en Render.")
        return
    try:
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        answer = "\n".join(part.get("text", "") for part in parts if part.get("text"))
        if not answer.strip():
            feedback = data.get("promptFeedback", {})
            log.warning("Respuesta Gemini vacía: %s", feedback)
            send_message(chat_id, "Gemini respondió sin texto. Prueba otra vez; si persiste, revisa los logs de Render.")
            return
        send_message(chat_id, "🤖 EL GRAN GOLPE · GEMINI\n\n" + answer.strip())
    except Exception as exc:
        log.warning("No se pudo procesar respuesta Gemini: %s", exc)
        send_message(chat_id, "Gemini respondió, pero no pude leer la respuesta. Revisa los logs de Render.")


def help_text() -> str:
    return (
        "🏁 EL GRAN GOLPE v1.9 CORREGIDO + GEMINI\n\n"
        "/start — activar mensajes en este chat\n/scan — barrido de cuotas actuales (consume créditos)\n"
        "/stats — tendencias estadísticas API-Football (no son cuotas ni picks confirmados)\n"
        "/pick_hoy — analizar el mejor candidato guardado\n/combo — combinada con señales guardadas\n"
        "/status — estado de configuración\n/results — rendimiento registrado\n"
        "/settle ID won|lost|push — liquidar un pick manualmente\n/help — ayuda\n\n"
        f"Cuota mínima: {MIN_ODDS:.2f}\nFiltro de diferencia vs consenso: {MIN_EDGE*100:.1f}%\n"
        f"Deportes configurados: {', '.join(SPORTS[:MAX_SPORTS_PER_SCAN])}\n"
        f"Mercados consultados: {MARKETS}; región: {REGIONS}\n\n"
        "Con API-Football: tendencias estadísticas de goles/ambos marcan. No incluye cuotas reales. Córners/tarjetas requieren consulta de estadísticas adicional; no se inventan datos."
    )


def handle_update(update: dict[str, Any]) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or not msg.get("text"):
        return
    sender_id = str((msg.get("from") or {}).get("id", ""))
    chat_id = msg["chat"]["id"]
    if AUTHORIZED_USER_ID and sender_id != AUTHORIZED_USER_ID:
        log.warning("Mensaje rechazado de usuario no autorizado: %s", sender_id)
        return
    text_in = msg["text"].strip()
    parts = text_in.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    if cmd == "/start":
        send_message(chat_id, "🤖 El Gran Golpe está conectado.\n\n" + help_text())
    elif cmd == "/help":
        send_message(chat_id, help_text())
    elif cmd == "/scan":
        if not ODDS_API_KEY:
            send_message(chat_id, "No puedo buscar picks actuales: falta ODDS_API_KEY. /stats solo consulta tendencias históricas y no sustituye cuotas actuales.")
        else:
            send_message(chat_id, "🔎 Iniciando barrido de cuotas actuales. Esto consume créditos de The Odds API.")
            try:
                scan(chat_id)
            except Exception as exc:
                send_message(chat_id, f"No se pudo completar el barrido: {str(exc)[:500]}")
    elif cmd == "/stats":
        if not API_FOOTBALL_KEY:
            send_message(chat_id, "Falta API_FOOTBALL_KEY en Render.")
        else:
            send_message(chat_id, "📊 Iniciando tendencias estadísticas. No consulta cuotas ni genera picks confirmados.")
            try:
                football_stats_scan(chat_id)
            except Exception as exc:
                send_message(chat_id, f"No se pudo completar el análisis estadístico: {str(exc)[:400]}")
    elif cmd == "/combo":
        combo(chat_id)
    elif cmd == "/pick_hoy":
        gemini_reply("Dame el mejor pick de hoy usando solo candidatos reales disponibles.", chat_id)
    elif cmd == "/status":
        send_message(chat_id,
            f"Estado: bot en ejecución\nIntervalo automático: {POLL_SECONDS//60} min\n"
            f"API-Football: {'clave presente' if API_FOOTBALL_KEY else 'FALTA CLAVE'}\n"
            f"The Odds API: {'clave presente' if ODDS_API_KEY else 'FALTA CLAVE'}\n"
            f"Gemini: {'clave presente' if GEMINI_API_KEY else 'FALTA CLAVE'}\n"
            f"Telegram: {'token presente' if BOT_TOKEN else 'FALTA TOKEN'}\n"
            f"Deportes por barrido: {', '.join(SPORTS[:MAX_SPORTS_PER_SCAN])}\nMercados: {MARKETS}; región: {REGIONS}\n"
            "Nota: clave presente no confirma cuota disponible ni permisos de temporada.")
    elif cmd == "/results":
        send_message(chat_id, performance())
    elif cmd == "/settle":
        settle(parts[1:], chat_id)
    else:
        gemini_reply(text_in, chat_id)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en Render.")
    if not API_FOOTBALL_KEY and not ODDS_API_KEY:
        log.warning("Faltan API_FOOTBALL_KEY y ODDS_API_KEY: el bot seguirá disponible, pero no podrá barrer datos deportivos.")
    offset = 0
    last_scan = time.time()  # Wait one full interval after startup before automatic scan.
    log.info("El Gran Golpe v1.9 corregido + Gemini iniciado; intervalo automático %ss; modelo %s", POLL_SECONDS, GEMINI_MODEL)
    while True:
        try:
            updates = telegram("getUpdates", {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": json.dumps(["message"])
            }) or []
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                handle_update(update)
        except Exception as exc:
            message = str(exc)
            log.warning("Telegram polling: %s", message)
            if "409" in message or "Conflict" in message:
                # Avoid crashing and triggering a Render restart loop. Telegram still
                # permits only one getUpdates poller per token; back off and retry slowly.
                log.critical(
                    "Telegram 409: otro proceso está usando el mismo token. "
                    "Este error no se corrige desde la API deportiva: deja una sola instancia "
                    "de Render y cierra cualquier ejecución local/servicio duplicado."
                )
                raise SystemExit("Telegram 409: poller duplicado; detener instancia duplicada.")
            else:
                time.sleep(4)

        # Automatic scans are intentionally infrequent; set POLL_SECONDS in Render.
        # For free API limits, prefer 21600 (6 h) or disable automatic scanning by
        # leaving ODDS_API_KEY blank until the monthly quota resets.
        if CHAT_ID and time.time() - last_scan >= POLL_SECONDS and ODDS_API_KEY:
            try:
                # Automatic picks always come from current odds, never from old-season stats.
                scan(CHAT_ID, report_summary=False)
            except Exception:
                log.exception("Barrido automático de cuotas falló")
            last_scan = time.time()


if __name__ == "__main__":
    main()
