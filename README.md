<div align="center">

# Glassistant

### Bringing Meta AI glasses to the wet lab.

<p align="center">
  <img src="https://img.shields.io/badge/Muse_Spark-1.3-0866FF?style=for-the-badge&amp;logo=meta&amp;logoColor=white" alt="Muse Spark 1.3" />
  <img src="https://img.shields.io/badge/Muse_Glimmer-30B-0866FF?style=for-the-badge&amp;logo=meta&amp;logoColor=white" alt="Muse Glimmer 30B" />
  <img src="https://img.shields.io/badge/SAM-3.1-0866FF?style=for-the-badge&amp;logo=meta&amp;logoColor=white" alt="SAM 3.1" />
  <img src="https://img.shields.io/badge/Meta_Wearables_DAT-1.0-0866FF?style=for-the-badge&amp;logo=meta&amp;logoColor=white" alt="Meta Wearables DAT 1.0" />
</p>

Prototyped with **Ray-Ban Meta**, with **Oakley Meta Vanguard** planned for device validation, Glassistant brings hands-free assistance, visual context, and structured experiment records to the lab bench.

</div>

<p align="center">
  <img src="01-connection.gif" alt="Glassistant launch and Meta glasses connection" width="800" />
</p>

<p align="center">
  <a href="https://x.com/MetaforDevs/status/2103237819993137592/photo/1">
    <img src="https://pbs.twimg.com/media/HTAzjq9b0AA7RpN.png" alt="Meta Global AI Developer Hackathon — announcement from Meta for Developers" width="900" />
  </a>
</p>

<p align="center"><sub>Built for the Meta Global AI Developer Hackathon · <a href="https://x.com/MetaforDevs/status/2103237819993137592/photo/1">Original announcement</a></sub></p>

<table width="100%">
  <tr>
    <td width="450" align="center" valign="middle">
      <a href="https://about.fb.com/news/2026/04/introducing-muse-spark-meta-superintelligence-labs/"><img src="https://meta.ai/images/meta-ai-orbit-logo-gradient-3d_light.svg" alt="Meta AI" width="64" /></a>
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lobehub/lobe-icons/refs/heads/master/packages/static-png/dark/metaai-text.png" />
        <img src="https://raw.githubusercontent.com/lobehub/lobe-icons/refs/heads/master/packages/static-png/light/metaai-text.png" alt="Meta AI" width="240" />
      </picture><br /><br />
      <sub>Muse Spark 1.3 · Muse Glimmer 30B · SAM 3.1</sub>
    </td>
    <td width="450" align="center">
      <a href="https://www.ray-ban.com/usa/ray-ban-meta-smart-glasses"><img src="https://images2.ray-ban.com//prod-onecp-record-files/pieyewear/38df2eee-3b46-4c23-b015-b3a10041c8e9/0RW4012__601S1Z__P21__shad__al31.png" alt="Ray-Ban Meta Wayfarer" width="440" /></a><br />
      <strong>Ray-Ban Meta</strong><br />
      <sub>Current prototype · Meta Wearables DAT 1.0</sub>
      <br /><br /><hr /><br />
      <a href="https://www.oakley.com/en-us/product/W0OW8001"><img src="https://assets2.oakley.com/cdn-record-files-pi/46591e70-a4ec-40b0-bbb5-b2fa00ff16e4/b39c0518-c99e-4558-a2c8-b3b700b40270/0OW8001__800107__P21__shad__cfr.png" alt="Oakley Meta Vanguard" width="440" /></a><br />
      <strong>Oakley Meta Vanguard</strong><br />
      <sub>Planned device validation · Meta Wearables DAT 1.0<br />
      IP67 dust and water resistance · Wrap-around design</sub>
    </td>
  </tr>
</table>

<sub>Both devices are <a href="https://developers.meta.com/wearables/faq/">supported by Meta Wearables DAT</a>; capabilities vary by device. <a href="https://www.meta.com/ai-glasses/oakley-meta-vanguard-black-prizm-black/">Vanguard’s IP67 rating</a> describes dust and water resistance, not laboratory eye-protection certification.</sub>

## Meet Milo

**My Intelligent Lab Observer** — your voice-based lab partner for tracking steps and recording what actually happened.

> “Hey Milo, I added Drug A to C2 through C4.”

## From protocol to bench

Import a protocol, then review the steps before starting a run.

<p align="center">
  <img src="02-import.gif" alt="Import a protocol and review the extracted steps" width="800" />
</p>

## Keep your hands on the experiment

Leave a note on a well, revisit it, and start a timer with Milo.

<p align="center">
  <img src="03-notes-timer.gif" alt="Record a note on D3, inspect the well, and start a timer" width="800" />
</p>

## From bench to robot

Preview the simulated workflow, inspect the plate map and robot instructions, then confirm the handoff.

<p align="center">
  <img src="04-robot-export.gif" alt="Simulate the workflow, review CSV and Python outputs, and confirm export" width="800" />
</p>

## Ask without leaving the bench

Ask a question in the context of your experiment and open the answer alongside your run.

<p align="center">
  <img src="05-web-search.gif" alt="Milo web search interaction and answer review" width="800" />
</p>

*The animations show the interactive UI prototype, including simulated search, device connection, and robot export. Model integrations and device validation are in development. Badges show target versions; Glimmer 30B denotes model size.*

---

<p align="center">
  <sub>Built for the <a href="https://dev.meta.ai/events/global-hackathon">Meta Global AI Developer Hackathon</a>.<br />
  Images: <a href="https://x.com/MetaforDevs/status/2103237819993137592/photo/1">Meta for Developers</a> · <a href="https://about.fb.com/news/2026/04/introducing-muse-spark-meta-superintelligence-labs/">Meta</a> · <a href="https://www.oakley.com/en-us/product/W0OW8001">Oakley</a></sub>
</p>

## SAM well test

The Diagnostics SAM well test needs ffmpeg on the server's machine (it also runs
`ffprobe`), and OpenCV from `server/requirements.txt`:

```sh
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian, Ubuntu
```
