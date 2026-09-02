import unittest

from personal_finance_agent.aqbanking import parse_balance_output
from personal_finance_agent.security import validate_fints_pin


class SecurityTests(unittest.TestCase):
    def test_validate_fints_pin_accepts_ascii_under_limit(self):
        validate_fints_pin("abcDEF123!$%")

    def test_validate_fints_pin_rejects_long_value(self):
        with self.assertRaises(ValueError):
            validate_fints_pin("x" * 36)

    def test_validate_fints_pin_rejects_non_ascii(self):
        with self.assertRaises(ValueError):
            validate_fints_pin("pässword")

    def test_validate_fints_pin_rejects_empty_value(self):
        with self.assertRaises(ValueError):
            validate_fints_pin("")

    def test_validate_fints_pin_rejects_control_characters(self):
        for pin in ("abc\n123", "abc\r123", "abc\t123", "abc\x00123"):
            with self.subTest(pin=repr(pin)):
                with self.assertRaises(ValueError):
                    validate_fints_pin(pin)


class BalanceParserTests(unittest.TestCase):
    def test_parse_balance_output(self):
        output = "02.07.2026\t123.45 EUR\tDE001234\t12345678\t987654321\n"
        result = parse_balance_output(output, "2026-07-02")

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["date"], "2026-07-02")
        self.assertEqual(result.rows[0]["source"], "AqBanking")
        self.assertEqual(result.rows[0]["balance"], "123.45 EUR")
        self.assertEqual(result.rows[0]["iban"], "DE001234")
        self.assertEqual(result.diagnostics, ())


if __name__ == "__main__":
    unittest.main()
