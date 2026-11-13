"""Word accuracy with equivalent spoken integers/times normalized explicitly."""
import re
from step7_support import word_accuracy

_SMALL=('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split()
_TENS=('zero ten twenty thirty forty fifty sixty seventy eighty ninety').split()

def number_words(n):
    if n<20:return _SMALL[n]
    return _TENS[n//10]+(' '+_SMALL[n%10] if n%10 else '')

def spoken_text(text):
    text=re.sub(r'\b(\d{1,2}):(\d{2})\b',
                lambda m:number_words(int(m[1]))+' '+number_words(int(m[2])),text)
    return re.sub(r'\b\d{1,2}\b',lambda m:number_words(int(m[0])),text)

def speech_accuracy(expected,actual):
    return word_accuracy(spoken_text(expected),spoken_text(actual))
