"""IAB VMAP 1.0 manifest with inline VAST 3.0 ads: one linear mid-roll per selected break, each referencing
the catalogue creative (id, url, duration) chosen for that slot."""
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


def _hms(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _vast(brand: dict, creative: dict, media_url: str) -> str:
    return f"""<VAST version="3.0">
          <Ad id="{escape(creative['id'])}">
            <InLine>
              <AdSystem version="1.0">hoichoi-adbreak-planner</AdSystem>
              <AdTitle>{escape(brand['name'])}</AdTitle>
              <Advertiser>{escape(brand['name'])}</Advertiser>
              <Impression id="imp-{escape(creative['id'])}"><![CDATA[about:blank]]></Impression>
              <Creatives>
                <Creative id="{escape(creative['id'])}" sequence="1">
                  <Linear>
                    <Duration>{_hms(creative['duration_sec'])[:8]}</Duration>
                    <MediaFiles>
                      <MediaFile id="{escape(creative['id'])}" delivery="progressive" type="video/mp4" width="1280" height="720"><![CDATA[{media_url}]]></MediaFile>
                    </MediaFiles>
                  </Linear>
                </Creative>
              </Creatives>
            </InLine>
          </Ad>
        </VAST>"""


def build(breaks: list[dict]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<vmap:VMAP xmlns:vmap="http://www.iab.net/videosuite/vmap" version="1.0">']
    for i, b in enumerate(breaks, 1):
        parts.append(f"""  <vmap:AdBreak timeOffset="{_hms(b['t'])}" breakType="linear" breakId="midroll-{i}">
    <vmap:AdSource id="midroll-{i}-ad" allowMultipleAds="false" followRedirects="true">
      <vmap:VASTAdData>
        {_vast(b['brand'], b['creative'], b['media_url'])}
      </vmap:VASTAdData>
    </vmap:AdSource>
  </vmap:AdBreak>""")
    parts.append("</vmap:VMAP>")
    xml = "\n".join(parts) + "\n"
    ET.fromstring(xml.encode("utf-8"))  # refuse to emit a manifest that is not well-formed
    return xml
