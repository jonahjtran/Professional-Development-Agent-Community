import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Meter, Segmented, SwitchRow, Steps, money } from "./ui";

describe("ui", () => {
  it("formats money", () => {
    expect(money(1.5)).toBe("$1.50");
    expect(money(20, 0)).toBe("$20");
  });

  it("a segmented control reports the chosen option", async () => {
    const onChange = vi.fn();
    render(
      <Segmented name="a" value="full" onChange={onChange}
        options={[{ value: "full", label: "Full" }, { value: "echo", label: "Practice" }]} />,
    );

    await userEvent.click(screen.getByLabelText("Practice"));

    expect(onChange).toHaveBeenCalledWith("echo");
    expect(screen.getByLabelText("Full")).toBeChecked();
  });

  it("a switch row toggles", async () => {
    const onChange = vi.fn();
    render(<SwitchRow label="README" checked onChange={onChange} />);

    await userEvent.click(screen.getByRole("switch", { name: "README" }));

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("a meter shows the amount and turns orange then red", () => {
    const { rerender } = render(<Meter label="QA" spent={1} cap={2} warn={0.8} />);
    expect(screen.getByText("$1.00 of $2.00")).toBeInTheDocument();
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByRole("meter").querySelector("span.warn, span.over")).toBeNull();

    rerender(<Meter label="QA" spent={1.7} cap={2} warn={0.8} />);
    expect(screen.getByRole("meter").querySelector("span.warn")).not.toBeNull();

    rerender(<Meter label="QA" spent={2} cap={2} warn={0.8} />);
    expect(screen.getByRole("meter").querySelector("span.over")).not.toBeNull();
  });

  it("the stage strip marks each stage's state", () => {
    render(
      <Steps stages={[
        { stage: "architect", label: "Architect", state: "done", spent: 0, cap: 1, fraction: 0 },
        { stage: "qa", label: "QA", state: "waiting", spent: 0, cap: 1, fraction: 0 },
      ]} />,
    );

    expect(screen.getByText("Architect").closest(".step")).toHaveClass("done");
    expect(screen.getByText("QA").closest(".step")).toHaveClass("waiting");
  });
});
