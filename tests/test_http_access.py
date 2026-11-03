import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from expose_api import App, BearerAuth, ConfigurationError, MemoryStore, Principal
from asgi_client import request

try:
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class Note:
    id: str
    tenant: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"


class HTTPAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        async def verify(token):
            self.calls.append(token)
            await asyncio.sleep(0)
            return {"synthetic-a": Principal("first", "a"), "synthetic-b": Principal("second", "b")}.get(token)
        self.app = App(authenticate=BearerAuth(verify), auth_challenge="Bearer")
        self.resource = self.app.include(Note, store=MemoryStore(), path="/notes",
            authorize=lambda req: req.principal.subject != "blocked",
            record_policy=lambda principal, operation, value: value.tenant == principal.tenant)
        await self.resource.create({"id": "a", "tenant": "a"})
        await self.resource.create({"id": "b", "tenant": "b"})

    async def call(self, path="/notes", method="GET", *, token="synthetic-a", **kwargs):
        return await request(self.app, path, method,
            headers=[] if token is None else [(b"authorization", f"Bearer {token}".encode())], **kwargs)

    async def test_authentication_once_before_body_policy_or_storage_and_strict_parsing(self):
        for headers in ([], [(b"authorization", b"Basic invalid")],
                        [(b"authorization", b"Bearer a"), (b"Authorization", b"Bearer b")],
                        [(b"authorization", b"Bearer x\n")], [(b"authorization", b"Bearer "+ b"a"*8192)]):
            response = await request(self.app, method="POST", headers=headers, body=b"invalid")
            self.assertEqual((response.status, response.reads), (401, 0))
            self.assertEqual(response.headers[b"www-authenticate"], b"Bearer")
        self.assertEqual(self.calls, [])
        unknown = await self.call(method="POST", token="unknown", body=b"invalid")
        self.assertEqual((unknown.status, unknown.reads), (401, 0))
        self.assertEqual(self.calls, ["unknown"])
        success = await self.call()
        self.assertEqual(self.calls, ["unknown", "synthetic-a"])
        self.assertEqual(success.json, [{"id": "a", "title": "Initial"}])
        option = await self.call(method="OPTIONS", token=None)
        self.assertEqual(option.status, 204)
        self.assertEqual(self.calls, ["unknown", "synthetic-a"])
        head = await self.call("/notes/a", "HEAD")
        self.assertEqual((head.status, head.body), (200, b""))

    async def test_all_crud_tenant_isolation_and_concealed_records(self):
        a, b = await asyncio.gather(self.call(), self.call(token="synthetic-b"))
        self.assertEqual([row["id"] for row in a.json], ["a"])
        self.assertEqual([row["id"] for row in b.json], ["b"])
        missing = await self.call("/notes/missing")
        for method in ("GET", "PATCH", "DELETE"):
            response = await self.call("/notes/b", method, **({"data": {"tenant": "a"}} if method == "PATCH" else {}))
            self.assertEqual((response.status, response.body), (404, missing.body))
        denied = await self.call("/notes/a", "PATCH", data={"tenant": "b"})
        self.assertEqual(denied.status, 403)
        self.assertEqual((await self.resource.get("a")).tenant, "a")
        denied_create = await self.call(method="POST", data={"id": "c", "tenant": "b"})
        self.assertEqual(denied_create.status, 403)
        created = await self.call(method="POST", data={"id": "c", "tenant": "a"})
        self.assertEqual(created.status, 201)
        self.assertNotIn("tenant", created.json)
        self.assertEqual((await self.call("/notes/c", "PATCH", data={"title": "Saved"})).json["title"], "Saved")
        self.assertEqual((await self.call("/notes/c", "DELETE")).status, 204)
        self.assertEqual((await self.call("/notes/c")).status, 404)

    async def test_operation_denial_and_invalid_identity_callbacks_are_redacted(self):
        self.app.authenticate = lambda req: Principal("blocked", "a")
        denied = await self.call(method="POST", body=b"invalid")
        self.assertEqual((denied.status, denied.reads), (403, 0))
        for result in (False, {}, "synthetic private value"):
            self.app.authenticate = lambda req: result
            with self.assertLogs("expose_api", level="ERROR") as logs:
                response = await self.call()
            self.assertEqual(response.status, 500)
            self.assertNotIn("synthetic", response.body.decode() + str(logs.output))
        def failed(req):
            raise RuntimeError("synthetic private value")
        self.app.authenticate = failed
        with self.assertLogs("expose_api", level="ERROR") as logs:
            response = await self.call()
        self.assertNotIn("synthetic", response.body.decode() + str(logs.output))
        async def canceled(req):
            raise asyncio.CancelledError
        self.app.authenticate = canceled
        with self.assertRaises(asyncio.CancelledError):
            await self.call()

    async def test_scan_exhaustion_has_no_partial_values_and_openapi_matches_policy(self):
        app = App(authenticate=lambda req: Principal("first", "a"), auth_challenge="Custom")
        app.include(Note, store=self.resource.store, path="/notes", authorize=lambda req: True,
                    record_policy=lambda principal, op, value: True, scan_limit=1)
        response = await request(app)
        self.assertEqual((response.status, response.json["error"]["code"]), (503, "access_scan"))
        self.assertNotIn("Initial", response.body.decode())
        spec = self.app.openapi()
        get = spec["paths"]["/notes"]["get"]
        self.assertEqual(get["security"], [{"BearerAuth": []}])
        self.assertEqual(get["x-access-policy"], "authenticated-record-policy")
        self.assertEqual(get["x-access-scan-limit"], 10000)
        self.assertTrue({"401", "403", "404", "503"} <= get["responses"].keys())
        self.assertEqual(spec["components"]["securitySchemes"]["BearerAuth"], {"type": "http", "scheme": "bearer"})
        self.assertNotIn("securitySchemes", app.openapi()["components"])

    async def test_legacy_and_public_mounts_do_not_authenticate(self):
        def unexpected(req):
            self.fail("public/legacy mount tried to authenticate")
        app = App(authenticate=unexpected, auth_challenge="Bearer")
        app.include(Note, store=MemoryStore(), authorize=lambda req: req.principal is None)
        self.assertEqual((await request(app)).status, 200)

    def test_configuration_rejects_incomplete_or_invalid_authentication(self):
        for kwargs in ({"authenticate": lambda req: None}, {"auth_challenge": "Bearer"},
                       {"authenticate": True, "auth_challenge": "Bearer"},
                       {"authenticate": lambda req: None, "auth_challenge": "Bearer\r\nsecret"}):
            with self.assertRaises(ConfigurationError):
                App(**kwargs)
        with self.assertRaises(ConfigurationError):
            App().include(Note, store=MemoryStore(), authorize=lambda req: True, record_policy=lambda *args: True)


@unittest.skipIf(BackpackStore is None, "optional Backpack development profile is not installed")
class PersistentHTTPAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_denials_rollback_and_preserve_committed_revision(self):
        with TemporaryDirectory() as directory:
            async with await BackpackStore.open(Note, Path(directory) / "notes.sqlite3", namespace="notes",
                                                table="app_notes", type_key="test.note") as store:
                app = App(authenticate=lambda req: Principal("first", "a"), auth_challenge="Custom")
                resource = app.include(Note, store=store, authorize=lambda req: True,
                    record_policy=lambda p, op, value: p.tenant == value.tenant)
                await resource.create({"id": "a", "tenant": "a"})
                await resource.create({"id": "b", "tenant": "b"})
                denied = await request(app, "/notes/a", "PATCH", data={"tenant": "b"})
                self.assertEqual(denied.status, 403)
                self.assertEqual((await store.get_row("a")).revision, "1")
                self.assertEqual((await request(app, "/notes/b", "DELETE")).status, 404)
                self.assertEqual([row["id"] for row in (await request(app)).json], ["a"])
