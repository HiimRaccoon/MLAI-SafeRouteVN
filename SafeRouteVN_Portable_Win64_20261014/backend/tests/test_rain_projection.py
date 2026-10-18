import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from backend.services.session_service import SessionService


@pytest.mark.parametrize('shift', [0, 11 * 3600])
@pytest.mark.parametrize('phase', ['pending', 'active', 'expired'])
def test_rain_location_is_bound_to_applied_overlay_and_simulated_interval(shift, phase):
    fixture = json.loads((Path(__file__).resolve().parents[2] /
        'scenarios/fixtures/thu-duc-binh-thanh-v1/S4.json').read_bytes())
    event = fixture['events'][0]
    start = datetime.fromisoformat(event['startTime']) + timedelta(seconds=shift)
    end = datetime.fromisoformat(event['endTime']) + timedelta(seconds=shift)
    view = dict(basis={'overlay_sha256': None if phase == 'pending' else 'a' * 64},
                execution_mode='SIMULATED_REPLAY', real_world_observation=False,
                current_time=(end if phase == 'expired' else start).isoformat(),
                order_ids=[o['id'] for o in fixture['initialState']['orders']],
                pending_event_ids=[event['eventId']] if phase == 'pending' else [])
    if shift:
        view['delivery_day'] = {'event_shift_seconds': shift,
            'catalog': {}, 'depots': []}
    service = SessionService.__new__(SessionService)
    service.session = lambda sid: {}
    service.fixture = lambda row: fixture
    result = asyncio.run(service.projection('session-test', 'locations', view=view))
    if phase == 'active':
        assert result['rain'] == {'polygon': event['polygon'], 'ends_at': end.isoformat()}
    else:
        assert result['rain'] is None
