import unittest
from cogs.llm_tools import _MojeekResultsParser

class MojeekResultsParserTests(unittest.TestCase):
    def test_extracts_bounded_organic_results(self):
        parser = _MojeekResultsParser(1)
        parser.feed('''<ul><li class="r1"><h2><a class="title" href="https://docs.example/a"> First &amp; Best </a></h2><p class="s"> Useful <strong>current</strong> documentation. </p></li><li class="r2"><h2><a class="title" href="https://docs.example/b">Second</a></h2></li></ul>''')
        self.assertEqual(parser.results, [{"title": "First & Best", "url": "https://docs.example/a", "snippet": "Useful current documentation."}])

    def test_rejects_non_http_result_links(self):
        parser = _MojeekResultsParser(5)
        parser.feed('<li class="r1"><h2><a class="title" href="javascript:bad">Bad</a></h2></li>')
        self.assertEqual(parser.results, [])

if __name__ == "__main__":
    unittest.main()
