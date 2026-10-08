/* Text that tells the reader to do something in Collie links Collie, at the workspace
   it is about, whenever that workspace's URL is known. */
(() => {
  // "Open in Collie" links as a phrase; elsewhere the word Collie is the link.
  const PHRASE = /Open in Collie|Collie/;
  window.collieText = (text, url) => {
    const fragment = document.createDocumentFragment();
    const value = String(text ?? "");
    const match = PHRASE.exec(value);
    let href = null;
    try {
      const parsed = new URL(url);
      if (["https:", "http:"].includes(parsed.protocol)) href = parsed.href;
    } catch {
      /* No workspace URL: the text stays plain. */
    }
    if (!match || !href) {
      fragment.append(value);
      return fragment;
    }
    const anchor = document.createElement("a");
    anchor.textContent = match[0];
    anchor.href = href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    fragment.append(
      value.slice(0, match.index),
      anchor,
      value.slice(match.index + match[0].length),
    );
    return fragment;
  };
})();
