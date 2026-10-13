import sqlite3

with sqlite3.connect("file:inventory.db?mode=ro", uri=True) as connection:
    rows = connection.execute("SELECT name FROM items WHERE stock = 0 ORDER BY name")
    for row in rows:
        print(row[0])
