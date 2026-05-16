import unittest

from scripts.calculate_cost import calculate_record_cost, filter_records, summarize


class CalculateCostTest(unittest.TestCase):
    def test_exact_alias_and_cached_token_rates(self):
        pricing = {
            "currency": "USD",
            "unit_tokens": 1000,
            "models": {
                "enterprise-gpt-5.5": {
                    "input": 1.0,
                    "input_cached_read": 0.25,
                    "input_cached_write": 1.5,
                    "cached_write_semantics": "subset",
                    "output": 2.0,
                }
            },
            "aliases": {"gpt-5.5-0424-global": "enterprise-gpt-5.5"},
        }
        record = {
            "response_model": "gpt-5.5-0424-global",
            "usage": {
                "input_tokens": 1000,
                "input_cached_read_tokens": 200,
                "input_cached_write_tokens": 100,
                "output_tokens": 500,
                "total_tokens": 1500,
            },
        }

        result = calculate_record_cost(record, pricing)

        self.assertTrue(result["matched"])
        self.assertAlmostEqual(result["cost"], 1.9)
        self.assertEqual(result["components"]["input_tokens"]["tokens"], 700)
        self.assertEqual(result["components"]["input_cached_read_tokens"]["tokens"], 200)
        self.assertEqual(result["components"]["input_cached_write_tokens"]["tokens"], 100)

    def test_pattern_rates_support_future_versions(self):
        pricing = {
            "currency": "USD",
            "unit_tokens": 1000,
            "models": {},
            "patterns": [
                {
                    "glob": "gpt-6.*-global",
                    "rates": {"input": 1.0, "output": 3.0},
                }
            ],
        }
        record = {
            "response_model": "gpt-6.1-global",
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }

        result = calculate_record_cost(record, pricing)

        self.assertTrue(result["matched"])
        self.assertAlmostEqual(result["cost"], 0.25)

    def test_additive_cached_token_semantics(self):
        pricing = {
            "currency": "USD",
            "unit_tokens": 1000,
            "cached_read_semantics": "additive",
            "cached_write_semantics": "additive",
            "models": {
                "model": {
                    "input": 1.0,
                    "input_cached_read": 0.5,
                    "output": 2.0,
                }
            },
        }
        record = {
            "response_model": "model",
            "usage": {
                "input_tokens": 100,
                "input_cached_read_tokens": 50,
                "output_tokens": 10,
            },
        }

        result = calculate_record_cost(record, pricing)

        self.assertTrue(result["matched"])
        self.assertAlmostEqual(result["cost"], 0.145)

    def test_naive_time_filters_are_treated_as_utc(self):
        records = [
            {"timestamp": "2026-05-16T00:00:00.000Z", "usage": {}},
            {"timestamp": "2026-05-17T00:00:00.000Z", "usage": {}},
            {"usage": {}},
        ]

        filtered = list(filter_records(records, "2026-05-16T12:00:00", "2026-05-18"))

        self.assertEqual(filtered, [records[1]])

    def test_regex_patterns_use_fullmatch(self):
        pricing = {
            "currency": "USD",
            "unit_tokens": 1000,
            "patterns": [{"pattern": "gpt-6", "rates": {"input": 1.0}}],
        }
        record = {"response_model": "prefix-gpt-6-suffix", "usage": {"input_tokens": 100}}

        result = calculate_record_cost(record, pricing)

        self.assertFalse(result["matched"])

    def test_summary_counts_unmatched_models(self):
        pricing = {"currency": "USD", "unit_tokens": 1000, "models": {}}
        summary = summarize(
            [
                {
                    "response_model": "unknown-model",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                }
            ],
            pricing,
            "auto",
        )

        self.assertEqual(summary["total"]["requests"], 1)
        self.assertEqual(summary["unmatched"], {"missing_price": 1})


if __name__ == "__main__":
    unittest.main()
