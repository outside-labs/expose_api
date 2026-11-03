# OpenAPI generation

`app.openapi(version="1.0", description="My application")` returns a fresh
[OpenAPI 3.1.0](https://spec.openapis.org/oas/v3.1.0.html) document for the mounted
resource methods. It performs no I/O and adds no public documentation route.
Serve or export the result explicitly under the application's access policy.
The application title supplies `info.title`; the caller owns its API version.
Customize the detached result without changing validation or registered routes.

Create, patch and response schemas derive from the same compiled field contracts
as runtime validation. They cover nested dataclasses, lists, string-keyed maps,
nullable unions, enums, literals and UUIDs. Create excludes read-only fields;
patch also excludes the immutable identity and makes only top-level fields
optional; response excludes write-only fields. Nested patch objects replace
complete values. Default factories are never invoked and default values are
omitted so documentation cannot expose private application defaults.

The document records selected CRUD/HEAD/OPTIONS methods, path identities,
offset/limit bounds, request body requirements, error envelopes and body limits.
Callback-protected methods are explicitly marked `x-access-policy: callback`.
It invents no identity provider or credential scheme. Authentication contracts
remain separately selected; callers may add their actual security scheme to
the returned document when they implement it.

JSON Schema cannot express Python constructor invariants, integer token spelling,
UTF-8 surrogate refusal or every backend constraint. `x-runtime-validation`
and `x-runtime-strict-integer` identify those runtime checks. Strict integer
validation rejects JSON `1.0` even though JSON Schema treats it as integral.
Custom stores can impose a narrower persistence profile; their validation and
availability errors remain runtime behavior. Schema generation never promises
that a schema-shaped value passes every application constructor or transaction.
