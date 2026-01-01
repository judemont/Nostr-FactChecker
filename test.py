import unittest
from factchecker import FactChecker
import os
import logging
import sys


class TestFactChecker(unittest.TestCase):
    def test_factchecker_check_fact(self):

        
        MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

        if MISTRAL_API_KEY is None:
            raise ValueError("MISTRAL_API_KEY environment variable not set")

        FACTCHECKER_AGENT_ID = "ag_019b704bddcc72079c3a26f9cb4891fa"

        try:
            factchecker = FactChecker(agent_id=FACTCHECKER_AGENT_ID, api_key=MISTRAL_API_KEY)
            statement = """IBS (Irritable Bowel Syndrome) affects 10-15% of the population.

The standard medical advice:
- Eat more fiber
- Try elimination diets
- Take probiotics
- Manage stress
- Accept it's chronic

What actually fixes IBS in majority of cases:
- Eliminate fiber and plants entirely.

Carnivore communities are full of former IBS sufferers who went from daily pain to zero symptoms within weeks.

Your "irritable bowel" isn't malfunctioning. It's responding appropriately to irritants.

Remove the irritants (fiber, lectins, FODMAPs, oxalates) and the irritation stops.

But doctors can't recommend "stop eating vegetables" because:
- It contradicts guidelines
- No pharmaceutical company sponsors that advice
- They'd be accused of promoting an "extreme" diet

So instead: Lifelong condition, manage symptoms, here's a prescription.

Rather than: Stop eating the things irritating your bowel."""
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