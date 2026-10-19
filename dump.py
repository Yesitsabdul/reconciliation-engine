import sqlite3
c = sqlite3.connect('reconciliation.db')
print(c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='manual_reviews'").fetchone()[0])
