from executor import Executor, POLY_MIN_NOTIONAL


class FakeMetadataClient:
    def get_clob_market_info(self, condition_id):
        assert condition_id == "0xabc"
        return {
            "mos": 7,
            "mts": 0.001,
            "fd": {"r": 0.07, "e": 1, "to": True},
            "t": [
                {"t": "up-token", "o": "Up"},
                {"t": "down-token", "o": "Down"},
            ],
        }


def test_executor_reads_clob_market_info_for_mos_mts_and_fee_rate():
    executor = Executor(private_key="", safe_address="", dry_run=False)
    executor.client = FakeMetadataClient()
    executor._initialized = True

    metadata = executor.get_market_metadata("0xabc")

    assert metadata.minimum_order_size == 7.0
    assert metadata.minimum_tick_size == 0.001
    assert metadata.fee_rate_bps == 700.0
    assert executor.min_order_size == 7.0
    assert executor.tick_size == 0.001


def test_executor_falls_back_to_static_min_notional_when_metadata_missing():
    executor = Executor(private_key="", safe_address="", dry_run=False)

    assert executor.min_order_size == POLY_MIN_NOTIONAL
