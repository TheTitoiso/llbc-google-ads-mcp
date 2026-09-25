"""
Test hors ligne de la couche HTTP de main.py (aucun appel a l'API Google Ads).

Ne fait PAS partie du serveur. Lancer depuis la racine du projet :

    python tests/http_auth_test.py

Le test fait passer de vraies requetes JSON-RPC par l'application ASGI `app`
et le transport MCP du SDK (streamable HTTP, sans etat), comme le fait le
connecteur claude.ai, et verifie :
  1. /health : public, marque, version, nombre de cles par role, outils, et
     aucun secret ni ID de compte dans la reponse ;
  2. les refus 401 (secret inconnu, trop court, absent, mauvais chemin) ;
  3. initialize : nom, version et instructions (marque, compte par defaut) ;
  4. tools/list selon le role : cle full = 23 outils, cle read = 10 outils de
     lecture (chemin /mcp/<secret> et en-tete Authorization: Bearer) ;
  5. tools/call : outil d'ecriture refuse a une cle read, compte par defaut,
     compte servi explicite, compte non servi refuse ;
  6. GOOGLE_ADS_ALLOW_WRITES=false : lecture seule pour toutes les cles.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

FULL_KEY = "cle-complete-0123456789abcdef"
READ_KEY = "cle-lecture-0123456789abcdef"
os.environ["AUTH_KEYS"] = f"{FULL_KEY}:thierry:full, {READ_KEY}:agence:read, court:x:full"
os.environ["GOOGLE_ADS_ALLOW_WRITES"] = "true"
os.environ["GOOGLE_ADS_CUSTOMER_ID"] = "123-456-7890, 111-222-3333"
os.environ["BRAND_NAME"] = "Marque test"
for _var in (
    "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "MCP_ALLOWED_HOSTS",
):
    os.environ.pop(_var, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402  (dependance du SDK MCP)

import main  # noqa: E402  (import apres configuration de l'environnement)

CID, OTHER_CID = "1234567890", "1112223333"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
CHECKS = 0
QUERIES: list[tuple[str, str]] = []


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def fake_run_query(customer_id: str, query: str, limit: int = 200) -> list[dict]:
    QUERIES.append((customer_id, " ".join(query.split())))
    return [
        {
            "campaign": {"id": "42", "name": "Campagne test", "status": "ENABLED"},
            "campaign_budget": {"id": "7", "amount_micros": "25000000"},
        }
    ]


async def run() -> None:
    main.run_query = fake_run_query
    ids = iter(range(1, 10_000))
    transport = httpx.ASGITransport(app=main.app)
    # Le gestionnaire de sessions du SDK doit tourner (lifespan de l'app).
    async with main.mcp.session_manager.run():
        async with httpx.AsyncClient(transport=transport, base_url="https://lbc.test") as http:

            async def rpc(path: str, method: str, params: dict | None = None, headers: dict | None = None):
                body = {"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}}
                return await http.post(path, json=body, headers={**HEADERS, **(headers or {})})

            async def list_tools(path: str, headers: dict | None = None) -> list[dict]:
                r = await rpc(path, "tools/list", headers=headers)
                check(r.status_code == 200, f"tools/list {path} : {r.status_code} {r.text[:300]}")
                return r.json()["result"]["tools"]

            async def tool_names(path: str, headers: dict | None = None) -> list[str]:
                return sorted(t["name"] for t in await list_tools(path, headers))

            async def call_tool(path: str, name: str, arguments: dict) -> tuple[dict, str]:
                r = await rpc(path, "tools/call", {"name": name, "arguments": arguments})
                check(r.status_code == 200, f"tools/call {name} : {r.status_code} {r.text[:300]}")
                result = r.json()["result"]
                return result, result["content"][0]["text"]

            # 1) /health
            r = await http.get("/health")
            health = r.json()
            check(r.status_code == 200 and health["status"] == "ok", f"/health : {r.status_code}")
            check(health["service"] == "lbc-google-ads-mcp" and health["brand"] == "Marque test", f"marque : {health}")
            check(health["version"] == main.VERSION and health["writes_enabled"] is True, f"version : {health}")
            check(health["auth_keys"] == {"full": 1, "read": 1}, f"cle trop courte ignoree : {health['auth_keys']}")
            check(health["google_ads_configured"] is False and health["default_customer_configured"] is True, f"config : {health}")
            check(len(health["tools"]["read"]) == 10 and len(health["tools"]["write"]) == 13, f"outils : {health['tools']}")
            check(all(s not in r.text for s in (FULL_KEY, READ_KEY, CID, OTHER_CID)), "ni secret ni compte dans /health")
            check((await http.get("/")).json()["service"] == "lbc-google-ads-mcp", "/ = /health")

            # 2) Refus
            for path, headers in (
                ("/mcp/cle-inconnue-0123456789abcdef", None),
                ("/mcp/court", None),  # ignoree au chargement (moins de 16 caracteres)
                ("/mcp", None),
                ("/mcp", {"Authorization": "Bearer cle-inconnue-0123456789abcdef"}),
                ("/mcp", {"Authorization": FULL_KEY}),  # sans "Bearer "
                (f"/autre/{FULL_KEY}", None),
                (f"/mcp/{FULL_KEY}/x", None),
                (f"/mcp/{FULL_KEY[:-1]}", None),
            ):
                r = await rpc(path, "tools/list", headers=headers)
                check(r.status_code == 401 and r.json() == {"error": "unauthorized"}, f"{path} {headers} : {r.status_code}")

            # 3) initialize
            r = await rpc(
                f"/mcp/{FULL_KEY}",
                "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tests", "version": "0"}},
            )
            init = r.json()["result"]
            check(init["serverInfo"]["name"] == "lbc-google-ads-mcp", f"serverInfo : {init['serverInfo']}")
            check(init["serverInfo"]["version"] == main.VERSION, f"version : {init['serverInfo']}")
            instructions = init["instructions"]
            check("Marque test" in instructions and f"customer_id {CID}" in instructions, f"instructions : {instructions[:300]}")
            check(f"Other accounts served: {OTHER_CID}." in instructions, "autre compte servi annonce")
            check("list_accounts to discover" not in instructions, "pas de decouverte des comptes avec un compte par defaut")

            # 4) tools/list selon le role
            full_tools = await list_tools(f"/mcp/{FULL_KEY}")
            full = sorted(t["name"] for t in full_tools)
            read = await tool_names(f"/mcp/{READ_KEY}")
            check(len(full) == 23 and set(full) == main.READ_TOOL_NAMES | main.WRITE_TOOL_NAMES, f"cle full : {full}")
            check(read == sorted(main.READ_TOOL_NAMES) and len(read) == 10, f"cle read : {read}")
            check(await tool_names("/mcp", {"Authorization": f"Bearer {READ_KEY}"}) == read, "Bearer, cle read")
            check(await tool_names("/mcp", {"Authorization": f"bearer {FULL_KEY}"}) == full, "Bearer, cle full")
            check(await tool_names(f"/mcp/{FULL_KEY}/") == full, "barre finale toleree")
            for t in full_tools:
                check(t["annotations"]["readOnlyHint"] is (t["name"] in main.READ_TOOL_NAMES), f"readOnlyHint : {t['name']}")
                if t["name"] != "list_accounts":
                    check("customer_id" not in t["inputSchema"].get("required", []), f"customer_id facultatif : {t['name']}")

            # 5) tools/call
            write_args = {"campaign_id": "42", "status": "PAUSED", "dry_run": True}
            result, text = await call_tool(f"/mcp/{READ_KEY}", "set_campaign_status", write_args)
            check(result["isError"] is True and "lecture seule" in text, f"ecriture refusee a la cle read : {text}")

            QUERIES.clear()
            result, text = await call_tool(f"/mcp/{FULL_KEY}", "get_campaigns", {})
            check(result["isError"] is False and json.loads(text)["campaign_count"] == 1, f"get_campaigns : {text[:300]}")
            check(QUERIES[-1][0] == CID and "FROM campaign" in QUERIES[-1][1], f"compte par defaut : {QUERIES}")
            result, text = await call_tool(f"/mcp/{READ_KEY}", "get_campaigns", {"customer_id": "111-222-3333"})
            check(result["isError"] is False and QUERIES[-1][0] == OTHER_CID, f"compte servi explicite : {QUERIES}")
            result, text = await call_tool(f"/mcp/{FULL_KEY}", "get_campaigns", {"customer_id": "999-888-7777"})
            check("n'est pas servi" in json.loads(text)["error"]["message"] and len(QUERIES) == 2, f"compte non servi : {text}")

            # Cle full : l'outil d'ecriture est atteint (ici sans identifiants Google).
            result, text = await call_tool(f"/mcp/{FULL_KEY}", "set_campaign_status", write_args)
            check("Configuration Google Ads incomplete" in text, f"outil d'ecriture atteint : {text}")

            # 6) GOOGLE_ADS_ALLOW_WRITES=false : lecture seule pour toutes les cles
            main.ALLOW_WRITES = False
            try:
                check(await tool_names(f"/mcp/{FULL_KEY}") == read, "cle full sans ecritures = outils de lecture")
                result, text = await call_tool(f"/mcp/{FULL_KEY}", "set_campaign_status", write_args)
                check(result["isError"] is True and "desactivees" in text, f"ecritures desactivees : {text}")
                check((await http.get("/health")).json()["writes_enabled"] is False, "/health writes_enabled")
            finally:
                main.ALLOW_WRITES = True


def main_() -> None:
    asyncio.run(run())
    print(f"OK  couche HTTP (cles, roles, /health, compte par defaut)\n\n{CHECKS} verifications reussies, aucune requete reseau.")


if __name__ == "__main__":
    main_()
