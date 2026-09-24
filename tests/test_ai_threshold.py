import pytest

@pytest.mark.parametrize('params,extra,expected', [
    ({'decision_threshold': .6}, {'decision_threshold': .8}, (.6, 'request_params')),
    ({}, {'decision_threshold': .8}, (.8, 'response_extra')),
    ({'decision_threshold': None}, {}, (.5, 'default')),
    ({}, {'decision_threshold': True}, (.5, 'default')),
    ({'decision_threshold': 0}, {}, (0., 'request_params')),
    ({'decision_threshold': 1}, {}, (1., 'request_params')),
])
def test_resolution(params, extra, expected):
    from app.service.ai_threshold import resolve_effective_decision_threshold
    assert resolve_effective_decision_threshold({'extra': extra}, params) == expected

@pytest.mark.parametrize('value', [True, False, '0.5', -0.1, 1.1, float('nan'), float('inf'), [], {}])
def test_invalid_configuration_rejected(value):
    from app.service.ai_threshold import resolve_effective_decision_threshold
    from app.service.ai_service import AiService
    with pytest.raises(ValueError, match='decision_threshold'):
        resolve_effective_decision_threshold({}, {'decision_threshold': value})
    with pytest.raises(ValueError, match='decision_threshold'):
        AiService._normalize_params_json({'decision_threshold': value})
