"""Configuration validation endpoint: names variables, never values, and
covers only the modules of the active app (the futures / FX / grid modules
and the external bot control APIs were removed)."""


async def test_config_validation_endpoint_names_variables_only(client, auth_headers):
    assert (await client.get("/api/system/config-validation")).status_code == 401
    r = (await client.get("/api/system/config-validation", headers=auth_headers)).json()
    assert set(r["modules"]) == {"solana_fresh", "solana_migration", "solana_momentum", "alerts"}
    assert "SOLANA_RPC_URL is not set" in r["modules"]["solana_fresh"]["errors"]
