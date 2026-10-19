import urllib.request
import re

req = urllib.request.Request('https://haball.pk', headers={'User-Agent': 'Mozilla/5.0'})
try:
    html = urllib.request.urlopen(req).read().decode('utf-8')
    colors = set(re.findall(r'#([A-Fa-f0-9]{6})\b', html))
    print(colors)
except Exception as e:
    print(e)
