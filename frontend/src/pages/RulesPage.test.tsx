import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/mockApi";
import { RulesPage } from "./RulesPage";

const rules = {
  global: [{ id: "g1", description: "npm run build", uses: 3, last_used: "2026-11-06T01:00:00+00:00" }],
  projects: {
    demo: [{ id: "p1", description: "npx next …any arguments", uses: 0, last_used: null }],
    empty: [],
  },
};

function open(reply: unknown = rules) {
  const api = mockApi({ "GET /api/rules": reply, "POST /api/rules/revoke": { ok: true } });
  render(<MemoryRouter><RulesPage /></MemoryRouter>);
  return api;
}

describe("the rules page", () => {
  it("lists what is always allowed, everywhere and per project", async () => {
    open();

    expect(await screen.findByText("npm run build")).toBeInTheDocument();
    expect(screen.getByText("npx next …any arguments")).toBeInTheDocument();
    expect(screen.getByText(/used 3 times/)).toBeInTheDocument();
    expect(screen.getByText(/never used/)).toBeInTheDocument();
  });

  it("says when there are none", async () => {
    open({ global: [], projects: {} });

    expect(await screen.findByText(/Nothing is always allowed yet/)).toBeInTheDocument();
  });

  it("revokes a rule after asking", async () => {
    const api = open();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await screen.findByText("npx next …any arguments");

    await userEvent.click(screen.getAllByRole("button", { name: "Revoke" })[1]!);

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]?.body).toEqual({ scope: "project", project: "demo", id: "p1" });
  });

  it("revokes nothing if you cancel", async () => {
    const api = open();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await screen.findByText("npm run build");

    await userEvent.click(screen.getAllByRole("button", { name: "Revoke" })[0]!);

    expect(api.posts()).toHaveLength(0);
  });
});
