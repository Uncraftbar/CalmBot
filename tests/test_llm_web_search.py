import unittest
from cogs.llm_tools import _DuckDuckGoResultsParser, openai_tools, responses_tools


class DuckDuckGoResultsParserTests(unittest.TestCase):
    def test_extracts_and_decodes_bounded_organic_results(self):
        parser = _DuckDuckGoResultsParser(1)
        parser.feed("""
        <div class="result"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example%2Fa&amp;rut=x"> First &amp; Best </a></h2>
        <a class="result__snippet"> Useful <b>current</b> documentation. </a></div>
        <div class="result"><h2><a class="result__a" href="https://docs.example/b">Second</a></h2><a class="result__snippet">Other</a></div>
        """)
        self.assertEqual(parser.results, [{"title": "First & Best", "url": "https://docs.example/a", "snippet": "Useful current documentation."}])

    def test_standalone_tool_schemas_exclude_conversation_controls(self):
        openai_names = {item["function"]["name"] for item in openai_tools(
            False, include_conversation_control=False)}
        response_names = {item["name"] for item in responses_tools(
            False, include_conversation_control=False)}
        for names in (openai_names, response_names):
            self.assertNotIn("stay_silent", names)
            self.assertNotIn("end_conversation", names)
            self.assertIn("web_search", names)

    def test_rejects_non_http_result_links(self):
        parser = _DuckDuckGoResultsParser(5)
        parser.feed('<a class="result__a" href="javascript:bad">Bad</a><a class="result__snippet">Nope</a>')
        self.assertEqual(parser.results, [])


if __name__ == "__main__":
    unittest.main()
