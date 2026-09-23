from html.parser import HTMLParser
import sys

class Links(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for key, value in attrs:
                if key == "href":
                    print(value)

Links().feed(sys.stdin.read())
