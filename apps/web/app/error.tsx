"use client";

import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="page">
      <h2>Something went wrong</h2>
      <div className="error">{error.message || "An unexpected error occurred."}</div>
      <div style={{ marginTop: 16 }}>
        <button onClick={() => reset()}>Try again</button>
      </div>
    </div>
  );
}
