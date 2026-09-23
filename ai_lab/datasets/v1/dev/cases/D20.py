from converters import convert_record

def transform(records):
    return [convert_record(row) for row in records]
