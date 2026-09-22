from yonixalpha_core.execution_router import ExecutionProvider, RoutingContext, route


def test_binance_futures_routes_to_binance_executor():
    provider, reasons = route(RoutingContext(asset_class="binance_futures"))
    assert provider == ExecutionProvider.BINANCE_FUTURES
    assert reasons


def test_solana_without_confirmed_migration_is_unsupported():
    provider, reasons = route(RoutingContext(asset_class="solana", migration_confirmed=False))
    assert provider == ExecutionProvider.UNSUPPORTED
    assert any("bonding-curve" in r or "no verified" in r for r in reasons)


def test_solana_with_confirmed_migration_routes_to_jupiter():
    provider, reasons = route(RoutingContext(asset_class="solana", migration_confirmed=True))
    assert provider == ExecutionProvider.JUPITER
    assert reasons


def test_unknown_asset_class_is_unsupported():
    provider, reasons = route(RoutingContext(asset_class="something-else"))
    assert provider == ExecutionProvider.UNSUPPORTED
    assert any("unknown asset_class" in r for r in reasons)


def test_solana_default_migration_confirmed_is_false():
    """The default must be the conservative one — a caller that forgets to
    pass migration_confirmed should get UNSUPPORTED, never an accidental
    route to an executor that isn't actually wired up.
    """
    provider, _ = route(RoutingContext(asset_class="solana"))
    assert provider == ExecutionProvider.UNSUPPORTED
