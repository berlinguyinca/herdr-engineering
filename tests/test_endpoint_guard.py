"""validate_model_endpoint: what the LLM proxy may be told to connect to. Real name resolution (localhost), no mocks."""
import pytest

from herdr_engineering.endpoint_guard import validate_model_endpoint


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9000", "https://llm.example.test/v1", "http://llm-gateway:8080", "http://10.1.2.3:8000/",
    "http://[::1]:9000", "http://localhost:9000", "http://100.64.0.5:8080",
])
def test_private_and_loopback_service_urls_are_accepted(url):
    assert validate_model_endpoint(url) == url
    assert validate_model_endpoint(url, resolve=True) == url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data", "http://169.254.0.1", "https://169.254.169.254:443",
    "http://[fe80::1]/", "http://[fd00:ec2::254]/", "http://[::ffff:169.254.169.254]/",
    "http://100.100.100.200/", "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata.google.internal./", "http://0.0.0.0:8000", "http://[::]/",
    "http://2852039166/", "http://0xa9fea9fe/", "http://169.254.43518/",
])
def test_metadata_link_local_and_odd_spellings_are_refused(url):
    with pytest.raises(ValueError):
        validate_model_endpoint(url)


@pytest.mark.parametrize("url", [
    "ftp://127.0.0.1/", "file:///etc/passwd", "gopher://127.0.0.1:70/", "127.0.0.1:9000", "", None, 5,
    "http://user:pw@127.0.0.1:9000/", "http://user@127.0.0.1/", "http://127.0.0.1:9000@evil.example/",
    "http://127.0.0.1:99999/", "http:///path", "http://127.0.0.1 /x", "http://127.0.0.1/\nHost: x",
    "http://" + "a" * 3000,
])
def test_non_http_credentials_and_malformed_urls_are_refused(url):
    with pytest.raises(ValueError):
        validate_model_endpoint(url)


def test_a_name_is_checked_where_it_resolves_at_use_time():
    # localhost resolves to loopback: allowed. An unresolvable name is allowed too (the service may be down).
    assert validate_model_endpoint("http://localhost:1", resolve=True)
    assert validate_model_endpoint("http://no-such-host.invalid:1", resolve=True)
