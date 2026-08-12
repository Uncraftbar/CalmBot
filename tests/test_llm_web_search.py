import unittest
from cogs.llm_tools import _DuckDuckGoResultsParser


class DuckDuckGoResultsParserTests(unittest.TestCase):
    def test_extracts_and_decodes_bounded_organic_results(self):
        parser = _DuckDuckGoResultsParser(1)
        parser.feed("""
        <div class="result"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example%2Fa&amp;rut=x"> First &amp; Best </a></h2>
        <a class="result__snippet"> Useful <b>current</b> documentation. </a></div>
        <div class="result"><h2><a class="result__a" href="https://docs.example/b">Second</a></h2><a class="result__snippet">Other</a></div>
        """)
        self.assertEqual(parser.results, [{"title": "First & Best", "url": "https://docs.example/a", "snippet": "Useful current documentation."}])

    def test_rejects_non_http_result_links(self):
        parser = _DuckDuckGoResultsParser(5)
        parser.feed('<a class="result__a" href="javascript:bad">Bad</a><a class="result__snippet">Nope</a>')
        self.assertEqual(parser.results, [])


if __name__ == "__main__":
    unittest.main()
