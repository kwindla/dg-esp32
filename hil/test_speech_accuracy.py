import unittest
from speech_accuracy import speech_accuracy
class SpeechAccuracy(unittest.TestCase):
    def test_clock_format_is_equivalent(self):
        self.assertEqual(speech_accuracy('at three thirty','at 03:30'),1)
        self.assertEqual(speech_accuracy('at three thirty','at 3:30'),1)
    def test_wrong_time_and_missing_words_fail(self):
        self.assertLess(speech_accuracy('at three thirty','at 03:40'),1)
        self.assertLess(speech_accuracy('bring your pencil','bring pencil'),1)
    def test_integer_format_is_equivalent(self):
        self.assertEqual(speech_accuracy('twenty four notebooks','24 notebooks'),1)
if __name__=='__main__':unittest.main()
