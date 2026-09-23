import configparser

config = configparser.ConfigParser()
config.read("service.ini", encoding="utf-8")
host = config["service"]["host"]
port = config["service"].getint("port", fallback=8080)
print(f"{host}:{port}")
