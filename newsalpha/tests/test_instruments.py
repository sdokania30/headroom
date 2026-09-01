import pytest

from newsalpha.ingest.instruments import InstrumentMaster

DETAILED = """EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,ISIN,SERIES
NSE,E,11536,EQUITY,TCS,Tata Consultancy Services,1,0.05,INE467B01029,EQ
NSE,E,1594,EQUITY,INFY,Infosys Limited,1,0.05,INE009A01021,EQ
BSE,E,500209,EQUITY,INFY,Infosys Limited,1,0.05,INE009A01021,EQ
NSE,D,45678,OPTSTK,INFY,INFY 1500 CE,600,0.05,,
NSE,E,999,EQUITY,TTTCO,Trade To Trade Co,1,0.05,INE999A01011,BE
NSE,E,777,EQUITY,BIGLOT,Big Lot Co,50,0.10,INE777A01011,EQ
"""

# The compact file uses entirely different column names for the same data.
COMPACT = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_TICK_SIZE,SEM_SERIES
NSE,E,1594,EQUITY,INFY,1,0.05,EQ
"""


@pytest.fixture
def master():
    m = InstrumentMaster()
    m.load_from_text(DETAILED)
    return m


def test_resolves_symbol_to_security_id(master):
    assert master.resolve("INFY").security_id == "1594"


def test_reads_the_compact_column_names_too():
    """Dhan ships two files with different headers; both must index."""
    m = InstrumentMaster()
    assert m.load_from_text(COMPACT) == 1
    assert m.resolve("INFY").security_id == "1594"


def test_series_suffix_and_whitespace_are_tolerated(master):
    assert master.resolve("INFY-EQ").security_id == "1594"
    assert master.resolve(" infy ").security_id == "1594"


def test_derivatives_do_not_shadow_the_underlying(master):
    """An option written on INFY shares the symbol. Routing an equity signal to
    the option's securityId would buy something else entirely."""
    assert master.resolve("INFY").security_id == "1594"
    assert master.resolve("INFY").lot_size == 1


def test_same_symbol_on_both_exchanges_stays_distinct(master):
    assert master.resolve("INFY", "NSE_EQ").security_id == "1594"
    assert master.resolve("INFY", "BSE_EQ").security_id == "500209"


def test_lookup_by_bse_scrip_code(master):
    assert master.by_security_id("500209", "BSE_EQ").symbol == "INFY"


def test_lookup_by_isin(master):
    assert master.by_isin("INE467B01029").symbol == "TCS"
    assert master.by_isin("nonsense") is None


def test_enrich_prefers_a_known_security_id(master):
    assert master.enrich("", "500209", "BSE_EQ").security_id == "500209"
    assert master.enrich("INFY", "", "NSE_EQ").security_id == "1594"


def test_unknown_symbol_returns_none_rather_than_guessing(master):
    assert master.resolve("NOSUCHCO") is None


def test_trade_to_trade_series_is_flagged(master):
    """BE settles without intraday netting - an intraday strategy cannot exit."""
    assert not master.resolve("TTTCO").tradable_intraday
    assert master.resolve("INFY").tradable_intraday


def test_quantity_rounds_down_to_a_whole_lot(master):
    big = master.resolve("BIGLOT")
    assert big.round_quantity(137) == 100
    assert big.round_quantity(49) == 0  # below one lot is not an order
    assert master.resolve("INFY").round_quantity(137) == 137


def test_price_snaps_to_the_tick_grid(master):
    """Exchanges reject prices off the tick grid outright."""
    assert master.resolve("BIGLOT").round_price(101.237) == pytest.approx(101.20)
    assert master.resolve("INFY").round_price(101.237) == pytest.approx(101.25)


def test_malformed_rows_are_skipped_not_fatal():
    m = InstrumentMaster()
    text = DETAILED + "NSE,E,,EQUITY,,,notanumber,notanumber,,\n"
    assert m.load_from_text(text) == 5
    assert m.resolve("INFY") is not None


def test_reload_replaces_rather_than_accumulates(master):
    master.load_from_text(COMPACT)
    assert len(master) == 1
