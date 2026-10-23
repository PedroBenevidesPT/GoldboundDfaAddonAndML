"""The realm model is used only where it exists, and never silently."""
import numpy as np
import pandas as pd

from woweconomy.pricing.config import PricingConfig
from woweconomy.pricing.engine import PricingEngine


class Stub:
    def __init__(self, value=0.0, fail=False):
        self.value = value
        self.fail = fail

    def predict_net_unit(self, frame):
        if self.fail:
            raise RuntimeError("no")
        return np.asarray([self.value])


def engine(regional=100.0, realms=None):
    instance = PricingEngine(PricingConfig())
    instance.price_model = Stub(regional)
    instance.realm_models = realms or {}
    return instance


def frame():
    return pd.DataFrame([{"dbmarket": 1.0}])


def test_a_trained_realm_model_is_preferred():
    instance = engine(realms={"Dragonmaw": Stub(250.0)})
    price, scope = instance._predict_price("Dragonmaw", frame())
    assert price == 250.0
    assert scope == "realm"


def test_a_realm_without_a_model_uses_the_regional_one():
    instance = engine(realms={"Dragonmaw": Stub(250.0)})
    price, scope = instance._predict_price("Kazzak", frame())
    assert price == 100.0
    assert scope == "regional"


def test_a_broken_realm_model_falls_back_instead_of_inventing():
    instance = engine(realms={"Dragonmaw": Stub(fail=True)})
    price, scope = instance._predict_price("Dragonmaw", frame())
    assert price == 100.0
    assert scope == "regional"


def test_the_scope_is_recorded_on_the_estimate():
    instance = engine(realms={"Dragonmaw": Stub(250.0)})
    estimate = instance.estimate("Dragonmaw", 1)
    # no feature builder in this stub: the estimate is unavailable, but the
    # record must still expose the field the dataset writes
    assert "model_scope" in estimate.as_record()
