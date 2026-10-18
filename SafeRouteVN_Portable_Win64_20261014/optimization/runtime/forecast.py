"""Read-only certified forecast projection; never accepts or changes a job."""
from .job_view import job_view
from .protocol import require, wire_exact


def job_forecast(job):
    view = job_view(job)
    profile = job['request']['profile']
    require(profile in ('FASTEST', 'BALANCED', 'SAFER'), 'INVALID_DATA', 'profile', 'Unknown profile')
    trajectory = metrics = None
    if view['plan_available']:
        result = job['result']
        require(result['profile'] == profile, 'INVALID_DATA', 'profile', 'Result profile differs from request')
        trajectory = {'job_id': job['id'], 'profile': profile, 'forecast': True,
                      'domain_sha256': result['domain_sha256'], 'vehicle_routes': wire_exact(result['vehicle_routes'])}
        metrics = result['metrics']
    return {'schema_version': 'task02-m2-job-forecast/1', 'session_id': job['basis']['session_id'],
            'job_id': job['id'], 'profile': profile, 'input_basis': job['basis'],
            'build_sha256': job['basis']['build_sha256'], 'job_view': view,
            'trajectory': trajectory, 'metrics': metrics,
            'units': {'distance': 'm', 'duration': 's', 'action_time': 'us', 'cost': 'VND', 'mass': 'kg',
                      'geometry_crs': 'WGS84', 'geometry_order': 'longitude_latitude', 'exposure': 'PROXY'},
            'metric_scope': 'FORECAST_ONLY', 'execution_mode': 'SIMULATED_REPLAY', 'real_world_observation': False}
