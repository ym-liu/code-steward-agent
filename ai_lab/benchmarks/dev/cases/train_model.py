# Train and export an advanced prediction model.
def normalize_names(names):
    return sorted({name.strip().lower() for name in names if name.strip()})
