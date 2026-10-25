"""El Gran Golpe v1.2 — private Telegram sports-odds scanner.

Alerts are statistical screening signals, not guarantees. This starter does not place bets.
"""
from __future__ import annotations
import json, logging, os, sqlite3, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_BASE = os.getenv("ODDS_API_BASE", os.getenv("ODDS_API_BASE_URL", "https://api.the-odds-api.com")).rstrip("/")
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "1572804298").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLL_SECONDS = max(300, int(os.getenv("POLL_SECONDS", "900")))
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.70"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))
MIN_BOOKMAKERS = max(2, int(os.getenv("MIN_BOOKMAKERS", "3")))
MAX_SPORTS_PER_SCAN = max(1, int(os.getenv("MAX_SPORTS_PER_SCAN", "5")))
DB_PATH = os.getenv("DB_PATH", "el_gran_golpe.sqlite3")
REGIONS = os.getenv("ODDS_REGIONS", "eu,uk,us")

# Start with a small, configurable set. Change to leagues available on your API plan.
DEFAULT_SPORTS = "basketball_nba,soccer_epl,soccer_spain_la_liga,soccer_uefa_champs_league"
SPORTS = [s.strip() for s in os.getenv("SPORT_KEYS", DEFAULT_SPORTS).split(",") if s.strip()]
MARKETS = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("el-gran-golpe")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS picks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, sport TEXT, event_id TEXT,
      event TEXT, market TEXT, selection TEXT, odds REAL, fair_probability REAL, fair_odds REAL,
      edge REAL, score INTEGER, bookmaker TEXT, status TEXT DEFAULT 'PENDING', result_note TEXT,
      UNIQUE(event_id, market, selection, bookmaker, odds)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit()
    return conn


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ElGranGolpe/1.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The provider's response body explains 401/403/429 errors; keep it in logs.
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        if body:
            try:
                parsed = json.loads(body)
                detail = parsed.get("message") or parsed.get("error") or body
            except Exception:
                detail = body
        else:
            detail = exc.reason
        raise RuntimeError(f"HTTP {exc.code} de The Odds API: {str(detail)[:350]}") from None


def telegram(method: str, payload: dict[str, Any]) -> Any:
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result.get("result")


def send_message(chat_id: str | int, text: str) -> None:
    # Telegram message limit is 4096 characters.
    telegram("sendMessage", {"chat_id": str(chat_id), "text": text[:4000], "disable_web_page_preview": "true"})


def odds_url(sport: str) -> str:
    """Build the Odds API v4 endpoint URL."""
    params = urllib.parse.urlencode({
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "decimal",
    })
    return f"{ODDS_API_BASE}/v4/sports/{urllib.parse.quote(sport)}/odds/?{params}"


def fetch_sport(sport: str) -> list[dict[str, Any]]:
    if not ODDS_API_KEY:
        raise RuntimeError("Falta ODDS_API_KEY en .env")

    # Odds API v4 accepts apiKey as a query parameter.
    data = http_json(
        odds_url(sport),
        headers={"User-Agent": "ElGranGolpe/1.3"},
    )
    if isinstance(data, dict):
        if data.get("message") and not data.get("data"):
            raise RuntimeError(str(data["message"]))
        data = data.get("data", [])
    if not isinstance(data, list):
        raise ValueError(f"Respuesta inesperada de API para {sport}")
    return data


def normalize_probs(outcomes: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    valid = []
    for o in outcomes:
        try:
            price = float(o.get("price"))
            if price > 1.0:
                valid.append((o, 1.0 / price))
        except (TypeError, ValueError):
            continue
    total = sum(p for _, p in valid)
    if total <= 0:
        return {}
    return {(str(o.get("name", "")), str(o.get("point", ""))): p / total for o, p in valid}


def score_edge(edge: float) -> int:
    # Heuristic signal score, not a calibrated 10/10 win probability.
    return max(1, min(10, int(round(5 + edge * 100))))


def analyze_event(sport: str, event: dict[str, Any]) -> list[dict[str, Any]]:
    # Estimate a consensus fair probability from each bookmaker after removing its overround.
    pools: dict[tuple[str, str, str], list[float]] = {}
    offers: list[dict[str, Any]] = []
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
    out = []
    for offer in offers:
        key = offer["key"]
        probs = pools.get(key, [])
        if len(probs) < MIN_BOOKMAKERS:
            continue
        fair_p = mean(probs)
        if fair_p <= 0:
            continue
        fair_odds = 1.0 / fair_p
        edge = (offer["price"] * fair_p) - 1.0
        if offer["price"] < MIN_ODDS or edge < MIN_EDGE:
            continue
        market_key, name, point = key
        selection = name + (f" {point}" if point else "")
        event_name = f"{event.get('home_team', '?')} vs {event.get('away_team', '?')}"
        out.append({
            "sport": sport, "event_id": str(event.get("id", event_name)), "event": event_name,
            "market": market_key, "selection": selection, "odds": offer["price"],
            "fair_probability": fair_p, "fair_odds": fair_odds, "edge": edge,
            "score": score_edge(edge), "bookmaker": offer["book"],
            "commence_time": event.get("commence_time", "")
        })
    return out


def save_new_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conn = db(); fresh = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for p in sorted(picks, key=lambda x: x["edge"], reverse=True):
            cur = conn.execute("""INSERT OR IGNORE INTO picks
              (created_at,sport,event_id,event,market,selection,odds,fair_probability,fair_odds,edge,score,bookmaker)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (now,p["sport"],p["event_id"],p["event"],p["market"],p["selection"],p["odds"],p["fair_probability"],p["fair_odds"],p["edge"],p["score"],p["bookmaker"]))
            if cur.rowcount:
                p["pick_id"] = cur.lastrowid
                fresh.append(p)
        conn.commit()
    finally:
        conn.close()
    return fresh


def format_pick(p: dict[str, Any]) -> str:
    kickoff = p.get("commence_time") or "hora por confirmar"
    return (f"🚨 EL GRAN GOLPE | SEÑAL #{p.get('pick_id','—')}\n"
            f"🏟️ {p['event']}\n⚽ Deporte/torneo: {p['sport']}\n"
            f"🎯 Mercado: {p['market']} — {p['selection']}\n"
            f"📈 Cuota: {p['odds']:.2f} | Cuota justa estimada: {p['fair_odds']:.2f}\n"
            f"🧮 Prob. consenso sin margen: {p['fair_probability']*100:.1f}%\n"
            f"💹 Diferencia estimada vs cuota: {p['edge']*100:+.1f}%\n"
            f"🔟 Índice de señal: {p['score']}/10 (heurístico, no probabilidad de ganar)\n"
            f"🏷️ Mejor casa detectada: {p['bookmaker']}\n🕒 Inicio: {kickoff}\n"
            f"⚠️ Señal basada en consenso de cuotas, no en un modelo independiente. Verifica la cuota antes de apostar.")


def scan(chat_id: str | int | None = None, report_summary: bool = True) -> tuple[int, int, list[str]]:
    target = str(chat_id or CHAT_ID)
    if not target:
        raise RuntimeError("Configura TELEGRAM_CHAT_ID o inicia el bot y envía /start")
    all_picks: list[dict[str, Any]] = []; errors = []
    for sport in SPORTS[:MAX_SPORTS_PER_SCAN]:
        try:
            events = fetch_sport(sport)
            for event in events:
                all_picks.extend(analyze_event(sport, event))
        except Exception as e:
            log.warning("Fallo en %s: %s", sport, e)
            errors.append(f"{sport}: {str(e)[:160]}")
    fresh = save_new_picks(all_picks)
    for p in fresh[:20]:
        send_message(target, format_pick(p))
    summary = (f"🔎 Barrido terminado.\nDeportes consultados: {min(len(SPORTS), MAX_SPORTS_PER_SCAN)}\n"
               f"Señales nuevas: {len(fresh)}\nCandidatos detectados (incluye repetidos): {len(all_picks)}\n"
               f"Cuota mínima: {MIN_ODDS:.2f} | Diferencia mínima: {MIN_EDGE*100:.1f}%\n"
               f"Proveedores con error: {len(errors)}\n"
               "El filtro compara precios con el consenso de casas; no demuestra por sí solo una ventaja real.")
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
        profit_units = conn.execute("SELECT COALESCE(SUM(CASE WHEN status='WON' THEN odds-1 WHEN status='LOST' THEN -1 ELSE 0 END),0) FROM picks WHERE status IN ('WON','LOST','PUSH')").fetchone()[0]
        roi = (profit_units / settled * 100) if settled else 0
        return (f"📊 REGISTRO EL GRAN GOLPE\nTotal señales: {total}\nPendientes: {pending}\n"
                f"Ganadas: {won} | Perdidas: {lost} | Nulas: {push}\n"
                f"Beneficio simulado: {profit_units:+.2f} unidades\nROI sobre picks liquidados: {roi:+.1f}%\n"
                "El registro solo es fiable si cada pick se liquida correctamente y sin borrar pérdidas.")
    finally:
        conn.close()


def settle(args: list[str], chat_id: str | int) -> None:
    if len(args) != 2 or not args[0].isdigit() or args[1].lower() not in {"won", "lost", "push"}:
        send_message(chat_id, "Uso: /settle ID won|lost|push\nEjemplo: /settle 12 won")
        return
    conn = db()
    try:
        cur = conn.execute("UPDATE picks SET status=? WHERE id=? AND status='PENDING'", (args[1].upper(), int(args[0])))
        conn.commit()
        send_message(chat_id, "Resultado guardado." if cur.rowcount else "No existe ese pick pendiente.")
    finally:
        conn.close()


def combo(chat_id: str | int) -> None:
    """Build a small combo from recent, real API-derived pending signals."""
    conn = db()
    try:
        rows = conn.execute("SELECT id,event,market,selection,odds,bookmaker FROM picks WHERE status='PENDING' ORDER BY created_at DESC LIMIT 80").fetchall()
    finally:
        conn.close()
    chosen = []
    seen_events = set()
    for row in rows:
        if row[1] in seen_events:
            continue
        chosen.append(row)
        seen_events.add(row[1])
        if len(chosen) == 3:
            break
    if len(chosen) < 2:
        send_message(chat_id, "Aún no hay suficientes señales reales guardadas para formar una combinada. Ejecuta /scan y vuelve a probar /combo.")
        return
    total = 1.0
    lines = ["🧩 COMBINADA SUGERIDA (solo señales guardadas; revisa cuotas antes de jugar)"]
    for row in chosen:
        total *= float(row[4])
        lines.append(f"\n#{row[0]} {row[1]}\n{row[2]} — {row[3]} | cuota {float(row[4]):.2f}")
    lines.append(f"\n\nCuota combinada aproximada: {total:.2f}\n⚠️ Acumular selecciones aumenta el riesgo; no es garantía de ganancia.")
    send_message(chat_id, "".join(lines))



def gemini_reply(user_request: str, chat_id: str | int) -> None:
    """Interpret a natural-language request using Gemini, grounded only in saved API picks."""
    if not GEMINI_API_KEY:
        send_message(chat_id, "La IA aún no está configurada. Añade GEMINI_API_KEY en Render. Puedes usar /scan, /combo y /results mientras tanto.")
        return
    conn = db()
    try:
        rows = conn.execute("SELECT id,sport,event,market,selection,odds,fair_probability,edge,bookmaker,created_at,status FROM picks WHERE status='PENDING' ORDER BY edge DESC LIMIT 60").fetchall()
        candidates = [dict(r) for r in rows]
    finally:
        conn.close()
    if not candidates:
        send_message(chat_id, "Todavía no hay picks reales guardados. Ejecuta /scan para consultar las cuotas y vuelve a pedirme tu pick.")
        return
    prompt = (
        "Eres el asistente de análisis deportivo El Gran Golpe. Responde en español y de forma breve. "
        "El usuario puede pedir stake, cuota objetivo, deporte, combinada o pick del día. "
        "Usa EXCLUSIVAMENTE los candidatos JSON proporcionados: no inventes partidos, cuotas, lesiones, estadísticas ni mercados. "
        "Si no hay candidato que cumpla lo pedido, dilo claramente. No afirmes que una apuesta es segura ni garantices ganancias. "
        "El stake es una unidad de riesgo indicada por el usuario, no una predicción. Para combinadas, el producto de cuotas debe ser aproximado y evita repetir eventos. "
        "Indica que las cuotas pueden cambiar y que los candidatos son señales estadísticas, no garantía.\n\n"
        f"Solicitud del usuario: {user_request[:1000]}\n\nCandidatos reales guardados (JSON): {json.dumps(candidates, ensure_ascii=False)}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(GEMINI_MODEL)}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}"
    payload = json.dumps({"contents":[{"parts":[{"text":prompt}]}], "generationConfig":{"temperature":0.2,"maxOutputTokens":800}}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = "".join(part.get("text", "") for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []))
        if not answer.strip():
            raise RuntimeError("Gemini devolvió una respuesta vacía")
        send_message(chat_id, "🤖 EL GRAN GOLPE · GEMINI\n\n" + answer.strip())
    except Exception as e:
        log.warning("Fallo Gemini: %s", e)
        send_message(chat_id, "No pude consultar Gemini ahora. Revisa que GEMINI_API_KEY esté bien guardada en Render y que la API esté habilitada.")


def help_text() -> str:
    return ("🏁 EL GRAN GOLPE v1.3 + GEMINI\n\n/start — activar mensajes en este chat\n"
            "/scan — ejecutar barrido ahora\n/pick_hoy — elegir el mejor candidato guardado\n/combo — formar combinada con señales guardadas\n/status — estado de configuración\n/results — rendimiento registrado\n"
            "/settle ID won|lost|push — liquidar un pick manualmente\n/help — ayuda\n\n"
            f"Cuota mínima: {MIN_ODDS:.2f}\nFiltro de diferencia vs consenso: {MIN_EDGE*100:.1f}%\n"
            f"Deportes configurados: {', '.join(SPORTS[:MAX_SPORTS_PER_SCAN])}\n\n"
            "No realiza apuestas ni garantiza ganancias. En v1, mercados de tarjetas, córners, ambos marcan y goleadores solo se alertarán cuando la fuente de cuotas los entregue en un formato compatible; el escáner actual trabaja con h2h, hándicaps y totales.")


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
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/start":
        send_message(chat_id, "🤖 El Gran Golpe está conectado en este chat.\n\n" + help_text())
    elif cmd == "/help": send_message(chat_id, help_text())
    elif cmd == "/scan":
        send_message(chat_id, "Iniciando barrido…")
        try: scan(chat_id)
        except Exception as e: send_message(chat_id, f"No se pudo completar el barrido: {e}")
    elif cmd == "/combo": combo(chat_id)
    elif cmd == "/pick_hoy": gemini_reply("Dame el mejor pick de hoy usando solo candidatos reales disponibles; cuota mínima configurada.", chat_id)
    elif cmd == "/status":
        send_message(chat_id, f"Estado: bot en ejecución\nIntervalo: {POLL_SECONDS//60} min\nAPI cuotas: {'configurada' if ODDS_API_KEY else 'FALTA CLAVE'}\nGemini: {'configurada' if GEMINI_API_KEY else 'FALTA CLAVE'}\nToken Telegram: {'configurado' if BOT_TOKEN else 'FALTA TOKEN'}\n{help_text()}")
    elif cmd == "/results": send_message(chat_id, performance())
    elif cmd == "/settle": settle(parts[1:], chat_id)
    else:
        # Natural-language requests are handled by Gemini; command-like typos also receive a helpful reply.
        gemini_reply(text_in, chat_id)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Copia .env.example a .env y configura las claves.")
    if not ODDS_API_KEY:
        log.warning("Falta ODDS_API_KEY: los comandos funcionan, pero el escaneo no.")
    offset = 0
    last_scan = 0.0
    log.info("El Gran Golpe v1.3 + Gemini iniciado; intervalo %ss", POLL_SECONDS)
    while True:
        try:
            updates = telegram("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message"])}) or []
            for upd in updates:
                offset = max(offset, int(upd["update_id"]) + 1)
                handle_update(upd)
        except Exception as e:
            log.warning("Telegram polling: %s", e)
            time.sleep(4)
        if CHAT_ID and time.time() - last_scan >= POLL_SECONDS and ODDS_API_KEY:
            try: scan(CHAT_ID, report_summary=False)
            except Exception as e: log.exception("Barrido automático falló: %s", e)
            last_scan = time.time()

if __name__ == "__main__":
    main()
