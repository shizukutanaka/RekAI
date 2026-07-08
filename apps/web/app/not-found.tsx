import Link from "next/link";

export default function NotFound() {
  return (
    <div className="page">
      <h2>Page not found</h2>
      <p className="hint">
        There&apos;s nothing at this address. Head back to <Link href="/">Chat</Link> or
        pick another page from the nav above.
      </p>
    </div>
  );
}
