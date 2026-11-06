import { Link } from "react-router-dom";
import { Banner } from "../components/ui";

export function NotFound() {
  return (
    <>
      <Banner>There is nothing at this address.</Banner>
      <p><Link to="/">‹ Projects</Link></p>
    </>
  );
}
