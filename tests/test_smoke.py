from tests.conftest import login


def test_health(client):
    assert client.get("/health").data == b"ok"


def test_landing_page_offers_login(client):
    resp = client.get("/")
    assert "Přihlásit se" in resp.get_data(as_text=True)


def test_admin_sees_member_list(client, admin):
    login(client, admin)
    resp = client.get("/members/")
    assert resp.status_code == 200
    assert admin.name in resp.get_data(as_text=True)
