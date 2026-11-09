import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ProjectsPage } from "../pages/ProjectsPage";
import { mockApi } from "../test/mockApi";

function setup(routes: Record<string, unknown>) {
  const api = mockApi({ "GET /api/projects": [], ...routes });
  render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/p/:slug" element={<div>the project page</div>} />
      </Routes>
    </MemoryRouter>,
  );
  return api;
}

async function fill() {
  await userEvent.click(await screen.findByRole("button", { name: "New project" }));
  await userEvent.type(screen.getByLabelText(/^Name/), "my-project");
  await userEvent.type(screen.getByLabelText(/The idea/), "A tool that tracks student org events.");
}

const preview = (agent: string) => ({
  ok: true,
  clone: "/home/me/clones/my-project",
  fields: { name: "my-project", idea: "A tool that tracks student org events.", track: "engineering", agent, visibility: "private" },
});

describe("the new project popup", () => {
  it("is closed until the plus button is pressed, and the plus says it is expanded", async () => {
    setup({});
    const plus = await screen.findByRole("button", { name: "New project" });
    expect(plus).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(plus);

    expect(screen.getByRole("dialog", { name: "New project" })).toBeInTheDocument();
    expect(plus).toHaveAttribute("aria-expanded", "true");
  });

  it("closes with Cancel, with Escape and by pressing the plus again, and keeps the list behind it", async () => {
    setup({ "GET /api/projects": [] });
    const plus = await screen.findByRole("button", { name: "New project" });

    await userEvent.click(plus);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(plus);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(plus);
    await userEvent.click(plus);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("heading", { name: "Projects" })).toBeInTheDocument();
  });

  it("the empty list offers to start one in the same popup", async () => {
    setup({});

    await userEvent.click(await screen.findByRole("button", { name: "Start one" }));

    expect(screen.getByRole("dialog", { name: "New project" })).toBeInTheDocument();
  });
});

describe("new project", () => {
  it("sends what you typed to be checked, then shows exactly what a full run will do", async () => {
    const api = setup({ "POST /api/new/preview": preview("full") });
    await fill();

    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText("This will, on your behalf")).toBeInTheDocument();
    expect(screen.getByText("/home/me/clones/my-project")).toBeInTheDocument();
    expect(screen.getByText(/spend real money/)).toBeInTheDocument();
    expect(api.posts()[0]?.body).toMatchObject({
      name: "my-project", track: "engineering", agent: "full", visibility: "private",
    });
  });

  it("warns on the confirmation that a public repository is visible to everyone", async () => {
    setup({
      "POST /api/new/preview": {
        ...preview("full"),
        fields: { ...preview("full").fields, visibility: "public" },
      },
    });
    await fill();
    await userEvent.click(screen.getByLabelText("Public"));

    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText(/anyone on the internet will be able to see/)).toBeInTheDocument();
  });

  it("shows no such warning for a private repository", async () => {
    setup({ "POST /api/new/preview": preview("full") });
    await fill();

    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    await screen.findByText("This will, on your behalf");
    expect(screen.queryByText(/anyone on the internet/)).not.toBeInTheDocument();
  });

  it("a practice run says nothing is created or spent", async () => {
    setup({ "POST /api/new/preview": preview("echo") });
    await fill();
    await userEvent.click(screen.getByLabelText("Practice"));

    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText("This is a practice run")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is created on GitHub/)).toBeInTheDocument();
  });

  it("shows every problem the server found and keeps what you typed", async () => {
    setup({
      "POST /api/new/preview": { status: 400, body: { ok: false, problems: ["The name must be lowercase.", "Describe the idea in 10 to 2000 characters."] } },
    });
    await fill();

    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText("The name must be lowercase.")).toBeInTheDocument();
    expect(screen.getByText(/Describe the idea/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Name/)).toHaveValue("my-project");
  });

  it("goes back to the form from the confirmation", async () => {
    setup({ "POST /api/new/preview": preview("full") });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    await userEvent.click(await screen.findByRole("button", { name: "Back" }));

    expect(screen.getByLabelText(/^Name/)).toHaveValue("my-project");
  });

  it("confirming starts the run and opens the project", async () => {
    const api = setup({
      "POST /api/new/preview": preview("full"),
      "POST /api/new/start": { ok: true, slug: "my-project" },
    });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    await userEvent.click(await screen.findByRole("button", { name: "Create and start" }));

    expect(await screen.findByText("the project page")).toBeInTheDocument();
    expect(api.posts().map((p) => p.url)).toEqual(["/api/new/preview", "/api/new/start"]);
  });

  it("a repository that could not be created sends you back with the reason", async () => {
    setup({
      "POST /api/new/preview": preview("full"),
      "POST /api/new/start": { status: 409, body: { error: "my-project already exists on GitHub" } },
    });
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));

    await userEvent.click(await screen.findByRole("button", { name: "Create and start" }));

    expect(await screen.findByText("my-project already exists on GitHub")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument());
  });
});
