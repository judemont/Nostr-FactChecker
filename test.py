import json
import unittest
from factchecker import FactChecker
import os
import logging
import sys


class TestFactChecker(unittest.TestCase):
    def test_get_webpage_content(self):
        MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

        if MISTRAL_API_KEY is None:
            raise ValueError("MISTRAL_API_KEY environment variable not set")

        FACTCHECKER_AGENT_ID = "ag_019b704bddcc72079c3a26f9cb4891fa"
        max_length = 100
        try:
            factchecker = FactChecker(agent_id=FACTCHECKER_AGENT_ID, api_key=MISTRAL_API_KEY)
            url = "https://www.nasa.gov/earth/how-do-we-know-the-earth-isnt-flat-we-asked-a-nasa-expert-episode-53/"
            result = factchecker.get_webpage_content(url, max_length=max_length)
            content_dict = json.loads(result)
            self.assertIn("url", content_dict)
            self.assertIn("content", content_dict)
            self.assertEqual(content_dict["url"], url)
            self.assertIsInstance(content_dict["content"], str)
            self.assertLessEqual(len(content_dict["content"]), max_length)
            print(content_dict)
        except Exception as e:
            self.fail(f"FactChecker.get_webpage_content raised an exception: {e}")
    
    def test_web_search(self):
        MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

        if MISTRAL_API_KEY is None:
            raise ValueError("MISTRAL_API_KEY environment variable not set")

        FACTCHECKER_AGENT_ID = "ag_019b704bddcc72079c3a26f9cb4891fa"

        try:
            factchecker = FactChecker(agent_id=FACTCHECKER_AGENT_ID, api_key=MISTRAL_API_KEY)
            query = "Python programming language"
            results_json = factchecker.perform_web_search(query, num_results=2)
            results = json.loads(results_json)
            self.assertIsInstance(results, list)
            self.assertGreater(len(results), 0)
            print(results)
            for result in results:
                self.assertIn("title", result)
                self.assertIn("url", result)
                self.assertIn("body", result)

        except Exception as e:
            self.fail(f"FactChecker.perform_web_search raised an exception: {e}")
    
    def test_factchecker_check_fact(self):

        
        MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

        if MISTRAL_API_KEY is None:
            raise ValueError("MISTRAL_API_KEY environment variable not set")

        FACTCHECKER_AGENT_ID = "ag_019b704bddcc72079c3a26f9cb4891fa"

        try:
            factchecker = FactChecker(agent_id=FACTCHECKER_AGENT_ID, api_key=MISTRAL_API_KEY)
            statement = """
Astrology is a science that uses the positions of stars and planets to predict human affairs and natural phenomena. It has been scientifically proven to influence personality traits and life events.
"""
            result = factchecker.check_fact(statement)
            print("Fact check result:", result)
        except Exception as e:
            self.fail(f"FactChecker.check_fact raised an exception: {e}")
            
            
            
if __name__ == "__main__":
    logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="[%(asctime)s - %(levelname)s] %(message)s"
    )
    log = logging.getLogger("NostrFactCheckerBot")
    log.setLevel(logging.INFO)

    unittest.main()