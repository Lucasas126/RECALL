from passlib.hash import argon2

TEST_PASSWORD = "test-password-123"
TEST_PASSWORD_HASH = argon2.hash(TEST_PASSWORD)


def login(client, user_id: str):
    response = client.post(
        "/api/login",
        json={"user_id": user_id, "password": TEST_PASSWORD},
    )
    if response.status_code != 200:
        raise AssertionError(f"test login failed for {user_id!r}: {response.status_code} {response.text}")
    return response
