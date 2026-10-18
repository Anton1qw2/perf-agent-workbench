// markdown-it 15.0.3 is bundled locally; see markdown-it.LICENSE.txt.
// Raw HTML stays escaped. Keep the parser's built-in unsafe-link validation.
const messageMarkdown = typeof markdownit === 'function'
  ? markdownit({html: false, breaks: true, linkify: false, typographer: false})
  : null;
if (messageMarkdown) {
  // Messages must not cause unsolicited requests to remote image servers.
  messageMarkdown.renderer.rules.image = (tokens, index) =>
    messageMarkdown.utils.escapeHtml(tokens[index].content);
}

function renderMessageMarkdown(element, content) {
  if (messageMarkdown) element.innerHTML = messageMarkdown.render(content);
  else element.textContent = content;
}
