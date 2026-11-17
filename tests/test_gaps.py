import pytest

def test_entity_resolve_is_open_gap():
    from src.gazettetrack import entity_resolve
    with pytest.raises(NotImplementedError):
        entity_resolve.process({"id": "x"})

def test_versioning_is_open_gap():
    from src.gazettetrack import versioning
    with pytest.raises(NotImplementedError):
        versioning.process({"id": "x"})
