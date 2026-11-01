Optional font folder for video text.

Windows already has "Nirmala UI", which covers Telugu, Hindi, Tamil, Kannada, Malayalam and Bengali, so most
Windows PCs need nothing here. If the Video Studio says "Missing font for Telugu" (Linux, macOS, or a trimmed
Windows install):

  1. Download "Noto Sans Telugu" (free, SIL Open Font License) from https://fonts.google.com/noto
  2. Copy the .ttf file (for example NotoSansTelugu-Bold.ttf) into THIS folder.
  3. Restart the server and reload the page.

Any font file placed here is checked for real glyph coverage before it is used, so a font that does not
contain the script is ignored rather than drawing empty boxes.
