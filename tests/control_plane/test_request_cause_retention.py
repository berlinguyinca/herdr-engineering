from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import _make_handler


@pytest.mark.parametrize('original,status,expected', [
    ('TimeoutError',503,'TimeoutError'),
    (None,200,'BrokenPipeError'),
])
def test_client_disconnect_keeps_an_existing_dependency_failure(original,status,expected):
    records=[]
    metrics=SimpleNamespace(record=lambda *args:records.append(args),
                            route=lambda _path:'/api/v1/events')
    handler_type=_make_handler(SimpleNamespace(request_metrics=metrics))
    handler=object.__new__(handler_type)
    handler.path='/api/v1/events'

    def dispatch():
        handler._request_cause=original
        handler._response_status=status
        raise BrokenPipeError('client closed before the error response')

    with pytest.raises(BrokenPipeError):
        handler._timed_request(dispatch)
    assert len(records)==1
    assert records[0][2]==status
    assert records[0][3]==expected
