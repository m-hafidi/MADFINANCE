"""Offline tests for the bourse.ma fiche table parsing."""

import pytest

from madfinance.sources import bourse


class TestTableRows:
    def test_tags_stripped_empty_cells_dropped(self):
        html = ("<table><tr>"
                "<td> <b>ATW</b> </td><td></td>"
                "<td>Attijariwafa\u00a0bank</td>"
                "</tr></table>")
        assert bourse._table_rows(html) == [["ATW", "Attijariwafa bank"]]

    def test_multiple_rows(self):
        html = ("<table>"
                "<tr><th>Libellé</th><th>Valeur</th></tr>"
                "<tr><td>Cours</td><td>155,20</td></tr>"
                "</table>")
        assert bourse._table_rows(html) == [
            ["Libellé", "Valeur"],
            ["Cours", "155,20"],
        ]

    def test_empty(self):
        assert bourse._table_rows("") == []


class TestKvHelpers:
    def test_kv_row_pairs_and_quads(self):
        rows = [
            ["Date de clôture", "31/12/2025"],
            ["Raison sociale", "ATW", "Secteur", "Banques"],
        ]
        assert bourse._kv_row(rows) == {
            "date_de_cloture": "31/12/2025",
            "raison_sociale": "ATW",
            "secteur": "Banques",
        }

    def test_kv_key_folds_accents(self):
        assert bourse._kv_key("Date de clôture") == "date_de_cloture"
        assert bourse._kv_key("Nombre d'actions") == "nombre_d_actions"
        assert bourse._kv_key("ISIN") == "isin"


class TestIsoDate:
    def test_valid(self):
        assert bourse._iso_date("08/07/2026") == "2026-07-08"
        assert bourse._iso_date("31/12/2025") == "2025-12-31"
        assert bourse._iso_date(" 1/5/2024 ") == "2024-05-01"

    @pytest.mark.parametrize("text", ["", None, "N/A", "2025-12-31",
                                      "32/01/2025", "31/02/2025", "abc"])
    def test_invalid(self, text):
        assert bourse._iso_date(text) is None


class TestClassifyTable:
    def test_instrument_table(self):
        rows = [["ISIN", "MA0000011298"], ["Cours", "155,20"]]
        out = bourse._classify_table(rows)
        assert out == {"instrument": {"isin": "MA0000011298",
                                      "cours": "155,20"}}

    def test_company_table(self):
        rows = [["Société", "ATW"], ["Secteur", "Banques"]]
        out = bourse._classify_table(rows)
        assert out == {"company": {"societe": "ATW", "secteur": "Banques"}}

    def test_shareholders_table(self):
        rows = [["Déclarant", "Part en %"],
                ["Etisalat", "53,02"],
                ["Royaume du Maroc", "22,00"]]
        out = bourse._classify_table(rows)
        assert out == {"shareholders": [
            {"name": "Etisalat", "part_pct": 53.02},
            {"name": "Royaume du Maroc", "part_pct": 22.0},
        ]}

    def test_key_figures_matrix(self):
        rows = [["Indicateurs clés", "2024", "2023"],
                ["Chiffre d'affaires", "10 500", "9 800"]]
        out = bourse._classify_table(rows)
        assert out == {"key_figures": {
            "years": [2024, 2023],
            "rows": {"Chiffre d'affaires": ["10 500", "9 800"]},
        }}

    def test_key_figures_no_results(self):
        rows = [["Indicateurs clés", "2024"],
                ["Aucun résultat trouvé"]]
        out = bourse._classify_table(rows)
        assert out == {"key_figures": {"years": [2024], "rows": {}}}

    def test_dividends_table(self):
        rows = [["Année", "Montant (MAD)", "Type", "Date de détachement"],
                ["2024", "12,50", "Générale", "28/05/2025"]]
        out = bourse._classify_table(rows)
        assert out == {"dividends": [{
            "year": 2024,
            "amount": 12.5,
            "type": "Générale",
            "ex_date": "2025-05-28",
        }]}

    def test_capital_operations_table(self):
        rows = [["Date", "Nature de l'opération", "Nombre d'actions",
                 "Valeur nominale"],
                ["12/04/2023", "Augmentation de capital", "54 975 335", "100"]]
        out = bourse._classify_table(rows)
        assert out == {"capital_operations": [{
            "date": "2023-04-12",
            "nature": "Augmentation de capital",
            "share_change": 54975335.0,
            "par_value": 100.0,
        }]}

    def test_unknown_table(self):
        assert bourse._classify_table([["Foo", "Bar"], ["a", "b"]]) is None


class TestNormalizeSymbol:
    def test_trim_upper(self):
        assert bourse.normalize_symbol(" atw ") == "ATW"
