# generate_media(id, kind, subject, style, details=None)

Answers with the path the art will appear at; write that path into the game exactly. The art lands while you keep working.

For things the player should see call generate_media. It answers with the path the file will appear at; write that path into the game exactly. A roster of things is a loop over a list, not the same call written out many times.
id      a short name: letters, digits, - and _.
kind    "sprite" one thing cut out and drawn on top of the game — a key, a lamp, a coin. "actor" a thing with a body — a character, a creature, a vehicle — drawn as a sprite. "tile" a surface the game repeats, filling its frame: real ground or wall seen from directly above ("mossy forest floor with small stones, seen from directly above" — the words texture, seamless and pattern come back as noise). A tile is drawn small, so build it from large simple shapes with minimal fine detail, in muted low-contrast colours: the ground is the backdrop the sprites must stand out against, so it keeps the style's palette but never its vividness or bold outlines. "scene" a whole picture drawn behind everything. "mesh" a 3D model.
subject WHAT to draw, described for an artist, with no style words. Lead with the thing, then mood and lighting, then detail — trailing detail is what the artist drops. Name colours that stand out against the game's background.
style   the game's ONE style phrase — medium, palette, outline — written before the first call and passed identically on every one ("painted cartoon style, warm forest palette, soft dark outlines"). Stylized and cartoonish unless the request names another style.
details facts that are not prose. body: its body plan — biped, quadruped, or whatever it is (winged, wheeled, a box, a blob). view: how the game shows it — side, front, top-down, 3/4 top-down.
generate_media(id="knight", kind="actor", subject="a chibi knight in a silver helmet with a red plume, blue tunic, round wooden shield", style="chunky cartoon chibi style, bold flat colors, soft dark outlines", details={"body": "biped", "view": "side"})
