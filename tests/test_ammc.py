"""Offline tests for the AMMC pure helpers: folding, title detection, column
anchors and French value parsing. No network, no PDFs."""

from types import SimpleNamespace

import pytest

from madfinance import ammc


def W(text, x0, x1):
    """Minimal pdfplumber-like word dict for the coordinate helpers."""
    return {"text": text, "x0": x0, "x1": x1}


class TestFold:
    @pytest.mark.parametrize("text,expected", [
        ("Bilan consolidé au 31 décembre 2025", "bilan consolide au 31 decembre 2025"),
        ("ÉTAT DE RÉSULTAT GLOBAL", "etat de resultat global"),
        ("Réserves (autres)", "reserves autres"),
        ("Attijariwafa  bank", "attijariwafa bank"),
        (2025, "2025"),
    ])
    def test_folding(self, text, expected):
        assert ammc._fold(text) == expected


class TestStripMarker:
    @pytest.mark.parametrize("label,expected", [
        ("- Charges générales", ("Charges générales", True)),
        ("+ Produits d'exploitation", ("Produits d'exploitation", False)),
        ("• Autres", ("Autres", False)),
        ("* Les autres produits", ("Les autres produits", False)),
        ("Ventes nettes", ("Ventes nettes", False)),
    ])
    def test_markers(self, label, expected):
        assert ammc._strip_marker(label) == expected


class TestParseNum:
    @pytest.mark.parametrize("text,expected", [
        ("34 921 384", 34921384.0),
        ("(1 234)", -1234.0),
        ("(12,5)", -12.5),
        ("1.234.567", 1234567.0),   # dotted thousands
        ("1.234,5", 1234.5),        # dotted thousands + comma decimals
        ("1.5", 1.5),               # decimal dot
        ("12,5", 12.5),             # FR comma
        ("-9 832 500", -9832500.0),
        ("0", 0.0),
        ("-", None),
        ("n/d", None),
    ])
    def test_values(self, text, expected):
        assert ammc._parse_num(text) == expected


class TestColumnYear:
    @pytest.mark.parametrize("text,expected", [
        ("31/12/2025", 2025),
        ("1/1/2024", 2024),
        ("31-déc.-25", 2025),       # IAM dashed abbreviation, 2-digit year
        ("31-déc.-23", 2023),
        ("31-dec-2023", 2023),
        ("31 dec 2025", 2025),
        ("2025", 2025),
        ("1999", 1999),
        ("96", None),               # page numbers are not years
        ("120", None),
        ("125", None),
        ("Note", None),
        ("Bilan", None),
    ])
    def test_years(self, text, expected):
        assert ammc._column_year(text) == expected


class TestSectionOnLine:
    @pytest.mark.parametrize("line,kind", [
        # real statement titles
        ("Bilan consolidé au 31 décembre 2025 (en milliers de dirhams)", "balance"),
        ("Bilan au 31 décembre 2025 (en milliers de dirhams)", "balance"),
        ("ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE", "balance"),
        ("Bilan 31/12/2025 31/12/2024", "balance"),
        ("Compte de résultat consolidé au 31 décembre 2025 (en milliers de dirhams)",
         "income"),
        ("ÉTAT DE RÉSULTAT GLOBAL", "income"),
        ("ETAT DE RÉSULTAT GLOBAL CONSOLIDÉ", "income"),
        ("Comptes de résultat consolidés 2025 2024", "income"),
        ("TABLEAU DES FLUX DE TRÉSORERIE CONSOLIDÉS", "cashflow"),
        ("Tableau des flux de trésorerie au 31 décembre 2025 (en milliers de dirhams)",
         "cashflow"),
        ("Etat des flux de trésorerie 31/12/2025", "cashflow"),
    ])
    def test_titles_match(self, line, kind):
        assert ammc._section_on_line(line) == kind

    @pytest.mark.parametrize("line", [
        "Bilan Hors bilan7 Bilan Hors bilan",      # sidebar with glued digit
        "3. NOTES RELATIVES AU COMPTE DE RÉSULTAT",  # note heading with number
        "le tableau des flux de trésorerie pour l'exercice clos à cette date",
        "Bilan .............................. 95",  # table-of-contents leader
        "HORS BILAN au 31 décembre 2025 (en milliers de dirhams)",
        "Charge nette comptabilisée au compte de résultat -141 005 17 204",
        "Le présent rapport annuel couvre l'exercice 2025 et le compte de résultat "
        "des entités concernées tel que décrit ci-après dans les états annexés",
        "",
    ])
    def test_non_titles_rejected(self, line):
        assert ammc._section_on_line(line) is None


class TestTitleScore:
    @pytest.mark.parametrize("line,score", [
        # dated + consolidated: the real statements win
        ("Bilan consolidé au 31 décembre 2025 (en milliers de dirhams)", 3),
        ("ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE", 2),
        ("BILAN 31/12/2025 31/12/2024", 3),
        ("BILAN TOTAL", 1),
        ("Bilan 31/12/2025 31/12/2024", 2),
        ("État de la situation financière", 0),     # five-year summary line
    ])
    def test_scores(self, line, score):
        assert ammc._title_score(line) == score


class TestSectionPages:
    def _doc(self, n):
        """Fake pdfplumber document: .pages[i] is just the page index."""
        return SimpleNamespace(pages=list(range(n)))

    def test_slash_dated_continuation_included(self):
        # continuation pages carry slash dates (31/12/2025), which folding
        # collapses to "31 12 2025" — the raw text must be searched too
        texts = [
            "Compte de résultat consolidé au 31/12/2025 (en milliers de dirhams)",
            "Autres charges d'exploitation 31/12/2025 31/12/2024\nTotal 1 234",
            "Page sans dates de tableau",           # no table dates -> stop
        ]
        assert ammc._section_pages(0, "income", texts, self._doc(3)) == [0, 1]

    def test_month_named_continuation_still_included(self):
        texts = [
            "Compte de résultat consolidé au 31 décembre 2025",
            "Suite des charges 31 décembre 2025 31 décembre 2024",
        ]
        assert ammc._section_pages(0, "income", texts, self._doc(2)) == [0, 1]

    def test_other_statement_breaks_span(self):
        texts = [
            "Compte de résultat consolidé au 31 décembre 2025",
            "Suite 31 décembre 2025 31 décembre 2024",
            "Bilan consolidé au 31 décembre 2025 (en milliers de dirhams)",
        ]
        assert ammc._section_pages(0, "income", texts, self._doc(3)) == [0, 1]


class TestLocateSections:
    def test_best_score_first_divider_falls_through(self):
        pages = [
            # 0: divider page (statement names, no table) -> parses empty
            "SECTION DIVIDER\nETAT DE RÉSULTAT GLOBAL CONSOLIDÉ\n"
            "TABLEAU DES FLUX DE TRÉSORERIE CONSOLIDÉS",
            # 1: real balance
            "ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE",
            # 2: real income
            "ÉTAT DE RÉSULTAT GLOBAL",
            # 3: real cashflow
            "TABLEAU DES FLUX DE TRÉSORERIE CONSOLIDÉS",
        ]
        found = ammc._locate_sections(pages)
        assert found["balance"] == [1]
        assert found["income"] == [0, 2]    # divider scores 2, real page scores 1
        assert found["cashflow"] == [0, 3]  # tie -> divider first, then page 3

    def test_junk_and_empty_pages_skipped(self):
        pages = [
            "",
            "Bilan Hors bilan7 Bilan Hors bilan",
            "Compte de résultat consolidé au 31 décembre 2025 (en milliers de dirhams)",
        ]
        assert ammc._locate_sections(pages) == {"income": [2]}

    def test_dated_title_beats_summary(self):
        pages = [
            "État de la situation financière",           # 10-year summary
            "ÉTAT DE LA SITUATION FINANCIÈRE CONSOLIDÉE",  # real statement
        ]
        assert ammc._locate_sections(pages)["balance"] == [1, 0]


class TestLineAnchors:
    def test_slash_dates_header(self):
        lines = [
            [W("Attijariwafa", 40, 100), W("Rapport", 105, 145),
             W("Financier", 150, 195), W("2025", 480, 505), W("97", 510, 520)],
            [W("Compte", 40, 80), W("de", 84, 96), W("résultat", 100, 145),
             W("31/12/2025", 405, 457), W("31/12/2024", 492, 544)],
        ]
        anchors, idx = ammc._line_anchors(lines)
        assert idx == 1
        assert anchors == [(457, 2025), (544, 2024)]

    def test_dashed_dates_ascending(self):
        lines = [[
            W("Note", 40, 62),
            W("31-déc.-23", 300, 355),
            W("31-déc.-24", 420, 475),
            W("31-déc.-25", 540, 595),
        ]]
        anchors, idx = ammc._line_anchors(lines)
        assert idx == 0
        assert anchors == [(355, 2023), (475, 2024), (595, 2025)]

    def test_date_only_header_line(self):
        lines = [[W("31/12/2025", 405, 457), W("31/12/2024", 492, 544)]]
        assert ammc._line_anchors(lines) == ([(457, 2025), (544, 2024)], 0)

    @pytest.mark.parametrize("words", [
        # page footer: trailing bare year after a page number
        [W("Attijariwafa", 40, 100), W("Rapport", 105, 145),
         W("Financier", 150, 195), W("2025", 480, 505)],
        [W("Rapport", 40, 80), W("financier", 85, 130), W("2025", 480, 505),
         W("120", 510, 524)],
        # prose ending with a year
        [W("exercice", 40, 85), W("clos", 90, 112), W("au", 116, 130),
         W("31", 134, 148), W("décembre", 152, 200), W("2025", 204, 232)],
        # single full date inside a long lead-in (>= 10 words)
        [W("Le", 40, 52), W("tableau", 56, 92), W("présente", 96, 134),
         W("les", 138, 156), W("soldes", 160, 194), W("au", 198, 212),
         W("31/12/2025", 216, 268), W("tels", 272, 294), W("que", 298, 318),
         W("arrêtés", 322, 364)],
    ])
    def test_footers_and_prose_rejected(self, words):
        assert ammc._line_anchors([words]) == (None, None)


class TestLineRows:
    def test_label_overflow_and_value_groups(self):
        # whitespace splitting would merge label+values into a giant number;
        # coordinate grouping keeps the label out and one value per column
        words = [
            W("PRODUIT", 40, 78), W("NET", 84, 97), W("BANCAIRE", 103, 150),
            W("34", 405, 418), W("921", 422, 440), W("384", 444, 462),
        ]
        rows, orphan = ammc._line_rows(words, [(462, 2025)], 2.5, None)
        assert orphan is None
        assert rows == [("PRODUIT NET BANCAIRE", {2025: 34921384.0})]

    def test_note_column_collision_overwrites(self):
        # "Stocks 9 445 323 451": the note number shares the first anchor and
        # is overwritten by the real value (left-to-right assignment)
        words = [
            W("Stocks", 40, 74),
            W("9", 300, 306),          # note column, left of the dates
            W("445", 405, 430), W("323", 480, 505), W("451", 555, 580),
        ]
        rows, orphan = ammc._line_rows(
            words, [(410, 2023), (485, 2024), (560, 2025)], 2.5, None)
        assert rows == [("Stocks", {2023: 445.0, 2024: 323.0, 2025: 451.0})]

    def test_pending_label_consumed_by_values_only_line(self):
        words = [W("-1", 405, 418), W("237", 422, 440),
                 W("1", 480, 486), W("288", 490, 510)]
        rows, orphan = ammc._line_rows(
            words, [(440, 2023), (510, 2024)], 2.5, None,
            pending="Autres éléments du besoin du fonds de")
        assert rows == [(
            "Autres éléments du besoin du fonds de",
            {2023: -1237.0, 2024: 1288.0},
        )]

    def test_function_word_fragment_prepended(self):
        words = [W("provisions", 40, 96),
                 W("22", 300, 314), W("-7", 405, 418), W("691", 422, 440)]
        rows, orphan = ammc._line_rows(
            words, [(440, 2023)], 2.5, None,
            pending="Dotations nettes aux amortissements, dépréciations et aux")
        assert rows == [(
            "Dotations nettes aux amortissements, dépréciations et aux provisions",
            {2023: -7691.0},
        )]

    def test_label_only_line_returns_orphan(self):
        words = [W("roulement", 40, 92)]
        rows, orphan = ammc._line_rows(words, [(462, 2025)], 2.5, None)
        assert rows == []
        assert orphan == "roulement"

    def test_title_only_line_returns_label_orphan(self):
        # the page header line clears the pending before any row consumes it
        words = [W("ÉTAT", 40, 66), W("DE", 70, 84), W("RÉSULTAT", 88, 140)]
        rows, orphan = ammc._line_rows(words, [(462, 2025)], 2.5, None)
        assert rows == []
        assert orphan == "ÉTAT DE RÉSULTAT"

    def test_footer_label_skipped(self):
        words = [W("Rapport", 40, 80), W("financier", 85, 130),
                 W("2025", 480, 505), W("97", 510, 520)]
        rows, orphan = ammc._line_rows(words, [(462, 2025)], 2.5, None)
        assert rows == []
        assert orphan is None

    def test_negative_marker_line(self):
        words = [W("-", 40, 46), W("Charges", 50, 94),
                 W("91", 405, 418), W("628", 422, 440)]
        rows, orphan = ammc._line_rows(words, [(440, 2025)], 2.5, None)
        assert rows == [("Charges", {2025: -91628.0})]


class TestTypeRank:
    @pytest.mark.parametrize("name,rank", [
        ("Rapports annuels", 0),
        ("Rapport annuel consolidé", 1),
        ("Rapport annuel", 2),
        ("Rapport semestriel", 3),
        ("Rapports semestriels", 3),
        ("Rapport trimestriel", 4),
        ("", 4),
    ])
    def test_ranks(self, name, rank):
        assert ammc._type_rank(name) == rank


class TestParseListHtml:
    HTML = """
    <table class="views-table">
      <tr>
        <td><a href="/fr/espace-emetteurs/etats-financiers/awb-rfa-2025">PDF</a></td>
        <td>ATTIJARIWAFA BANK</td><td>2025</td><td>Rapports annuels</td>
      </tr>
      <tr>
        <td><a href="/fr/actualite">Actu</a></td>
        <td>NOT A REPORT</td><td>2024</td><td>Rapports annuels</td>
      </tr>
      <tr>
        <td><a href="/fr/espace-emetteurs/etats-financiers/x">PDF</a></td>
        <td>NO YEAR</td><td>Rapports annuels</td>
      </tr>
    </table>
    """

    def test_rows(self):
        rows = ammc.parse_list_html(self.HTML)
        assert rows == [{
            "issuer": "ATTIJARIWAFA BANK",
            "year": 2025,
            "type": "Rapports annuels",
            "url": ammc.BASE + "/fr/espace-emetteurs/etats-financiers/awb-rfa-2025",
        }]

    def test_empty_html(self):
        assert ammc.parse_list_html("<html><body>nothing</body></html>") == []
