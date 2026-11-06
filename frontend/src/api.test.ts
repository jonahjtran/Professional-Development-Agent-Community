import { describe, expect, it } from "vitest";
import { ApiError, get, post } from "./api";
import { mockApi } from "./test/mockApi";

describe("api", () => {
  it("sends changes as JSON with the session token, fetched once", async () => {
    const api = mockApi({ "POST /api/x": { ok: true } });

    await post("/api/x", { a: 1 });
    await post("/api/x", { a: 2 });

    const [first, second] = api.posts();
    expect(first?.body).toEqual({ a: 1 });
    expect(first?.headers).toMatchObject({ "content-type": "application/json", "x-csrf-token": "tok" });
    expect(second?.body).toEqual({ a: 2 });
    expect(api.calls.filter((c) => c.url === "/api/session")).toHaveLength(1);
  });

  it("turns an error reply into an ApiError carrying the server's words", async () => {
    mockApi({ "POST /api/x": { status: 409, body: { error: "too late" } } });

    await expect(post("/api/x")).rejects.toMatchObject({ status: 409, message: "too late" });
  });

  it("keeps a list of problems from a refused form", async () => {
    mockApi({ "POST /api/x": { status: 400, body: { ok: false, problems: ["bad name", "short idea"] } } });

    const error = await post("/api/x").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).problems).toEqual(["bad name", "short idea"]);
    expect((error as ApiError).message).toBe("bad name short idea");
  });

  it("reads with a plain GET and fails with the status when there is no message", async () => {
    mockApi({ "GET /api/y": { status: 404, body: {} } });

    await expect(get("/api/y")).rejects.toMatchObject({ status: 404, message: "Request failed (404)" });
  });
});
