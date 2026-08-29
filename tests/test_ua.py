from app.ua import describe_user_agent, detect_client_type


def test_detect_common_clients() -> None:
    assert detect_client_type(None) == "desktop"
    assert detect_client_type("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "desktop"
    assert detect_client_type("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)") == "desktop"
    assert detect_client_type("Mozilla/5.0 (X11; Linux x86_64)") == "desktop"
    assert detect_client_type("Mozilla/5.0 (Linux; Android 10; K) Mobile Safari/537.36") == "mobile"
    assert detect_client_type("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)") == "mobile"


def test_describe_user_agent() -> None:
    assert describe_user_agent(None) == "unknown"
    assert describe_user_agent("Windows NT 10.0") == "windows"
    assert describe_user_agent("Macintosh; Intel Mac OS X") == "macos"
    assert describe_user_agent("Linux; Android 14") == "android"
    assert describe_user_agent("iPhone; CPU iPhone OS") == "iphone"
