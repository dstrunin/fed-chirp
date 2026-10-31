import datetime as dt

from fed_chirp.fetchers import fomc
from fed_chirp.scoring.scorer import _doc_header, _scoring_directive


def test_fomc_presser_keeps_opening_page(monkeypatch):
    def fetch(url, *, drop_first_page):
        return 'page two' if drop_first_page else 'CHAIRMAN WARSH. Opening policy stance.\npage two'
    monkeypatch.setattr(fomc, 'fetch_pdf_text', fetch)
    ref = fomc.FomcRef('https://example.test/transcript.pdf', fomc.DOC_PRESSER, 'powell', dt.date(2026, 10, 28), 'Press conference')
    assert fomc.fetch_doc(ref).body.startswith('CHAIRMAN WARSH.')


def test_presser_scoring_does_not_assume_chair_identity():
    message = _doc_header(fomc.DOC_PRESSER, 'FOMC Chair', 'Chair', dt.date(2026, 10, 28)) + _scoring_directive(fomc.DOC_PRESSER)
    assert 'Powell' not in message
