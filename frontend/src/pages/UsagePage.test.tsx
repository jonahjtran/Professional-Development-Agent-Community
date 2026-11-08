import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/mockApi";
import { UsagePage } from "./UsagePage";

const plan = (over: Record<string, unknown> = {}) => ({
  as_of: "2026-11-08T15:00:00+00:00",
  age_seconds: 180,
  windows: [
    { name: "five_hour", label: "5-hour window", utilization: 0.2, resets_at: "2026-11-08T20:00:00+00:00", status: "allowed" },
    { name: "seven_day", label: "Weekly", utilization: 0.05, resets_at: "2026-11-14T12:00:00+00:00", status: "allowed" },
  ],
  overage: { status: "allowed", in_use: false },
  ...over,
});

const task = (over: Record<string, unknown> = {}) => ({
  task_id: "T1", model: "claude-sonnet-5-5", billing: "subscription",
  tokens: { input: 1200, output: 450, cache_read: 90000, cache_write: 3000 },
  change: { five_hour: 0.04, seven_day: 0.01 }, ts: "2026-11-08T14:00:00+00:00", ...over,
});

function report(over: Record<string, unknown> = {}) {
  return {
    plan: plan(),
    projects: [
      {
        slug: "demo",
        tasks: [task(), task({ task_id: "T2", change: {}, billing: "api" })],
        totals: { tasks: 2, tokens: { input: 2400, output: 900, cache_read: 180000, cache_write: 6000 }, change: { five_hour: 0.04, seven_day: 0.01 } },
      },
    ],
    ...over,
  };
}

function open(reply: unknown = report(), extra: Record<string, unknown> = {}) {
  const api = mockApi({ "GET /api/usage": reply, "POST /api/usage/refresh": { plan: plan({ age_seconds: 0 }) }, ...extra });
  render(<MemoryRouter><UsagePage /></MemoryRouter>);
  return api;
}

describe("the usage page", () => {
  it("shows how much of each plan window you have used and when it resets", async () => {
    open();

    const five = (await screen.findByText("5-hour window")).closest("li") as HTMLElement;
    expect(within(five).getByText("20%")).toBeInTheDocument();
    expect(within(five).getByText(/resets/i)).toBeInTheDocument();
    const week = screen.getByText("Weekly").closest("li") as HTMLElement;
    expect(within(week).getByText("5%")).toBeInTheDocument();
  });

  it("says how old the reading is and that it comes from Anthropic", async () => {
    open();

    expect(await screen.findByText(/3 minutes ago/)).toBeInTheDocument();
    expect(screen.getByText(/reported by Anthropic/i)).toBeInTheDocument();
  });

  it("flags a window that is close to its limit or has hit it", async () => {
    open(report({ plan: plan({ windows: [
      { name: "five_hour", label: "5-hour window", utilization: 0.92, resets_at: null, status: "allowed_warning" },
      { name: "seven_day", label: "Weekly", utilization: 1, resets_at: null, status: "rejected" },
    ] }) }));

    expect(await screen.findByText("Close to the limit")).toBeInTheDocument();
    expect(screen.getByText("Limit reached")).toBeInTheDocument();
  });

  it("says whether pay-as-you-go overage is in use", async () => {
    open(report({ plan: plan({ overage: { status: "allowed", in_use: true } }) }));

    expect(await screen.findByText(/overage is in use/i)).toBeInTheDocument();
  });

  it("lists each project's tasks with their real tokens and what they used of the plan", async () => {
    open();

    const row = (await screen.findByText("T1")).closest("tr") as HTMLElement;
    expect(within(row).getByText("1,200")).toBeInTheDocument(); // input tokens
    expect(within(row).getByText("450")).toBeInTheDocument(); // output tokens
    expect(within(row).getByText(/\+4% of the 5-hour window/)).toBeInTheDocument();
    expect(within(row).getByText(/\+1% of the weekly window/i)).toBeInTheDocument();
  });

  it("says when a task moved no window a whole percent or was on API credits", async () => {
    open();

    const row = (await screen.findByText("T2")).closest("tr") as HTMLElement;
    expect(within(row).getByText(/no change measured/i)).toBeInTheDocument();
    expect(within(row).getByText(/API credits/)).toBeInTheDocument();
  });

  it("totals a project's tokens and plan use", async () => {
    open();

    const total = (await screen.findByText(/2 tasks/)).closest("section") as HTMLElement;
    expect(within(total).getByText(/\+4% of the 5-hour window/)).toBeInTheDocument();
    expect(within(total).getByText(/2,400 in/)).toBeInTheDocument();
  });

  it("explains the limits of what is measured", async () => {
    open();

    expect(await screen.findByText(/whole percentages/i)).toBeInTheDocument();
    expect(screen.getByText(/dollar figures.*estimates/i)).toBeInTheDocument();
  });

  it("says nothing has been measured yet and offers to check now", async () => {
    open({ plan: null, projects: [] });

    expect(await screen.findByText(/No plan usage has been measured yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check now" })).toBeInTheDocument();
  });

  it("checks now, then shows the fresh reading", async () => {
    const api = open({ plan: null, projects: [] });

    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));

    await waitFor(() => expect(api.posts().map((p) => p.url)).toEqual(["/api/usage/refresh"]));
    expect(await screen.findByText("5-hour window")).toBeInTheDocument();
    expect(screen.getByText(/just now/i)).toBeInTheDocument();
  });

  it("shows why a check failed", async () => {
    open({ plan: null, projects: [] }, {
      "POST /api/usage/refresh": { status: 409, body: { error: "Claude Code reported no plan usage. Check that you are signed in." } },
    });

    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));

    expect(await screen.findByText(/reported no plan usage/)).toBeInTheDocument();
  });
});
