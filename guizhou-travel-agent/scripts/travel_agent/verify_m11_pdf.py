"""Inspect an exported guidebook's text and rasterize every page for review."""
import argparse
import json
from pathlib import Path
import subprocess

import pdfplumber
from PIL import Image


def verify(pdf,output,poppler):
    output.mkdir(parents=True,exist_ok=True)
    subprocess.run([str(poppler),"-r","112","-png",str(pdf),str(output/"page")],check=True,timeout=40)
    images=sorted(output.glob("page-*.png"))
    pages=[]
    with pdfplumber.open(str(pdf)) as document:
        for page,image in zip(document.pages,images,strict=True):
            text=page.extract_text() or ""
            words=page.extract_words()
            bounds=[min(w["x0"] for w in words),min(w["top"] for w in words),
                    max(w["x1"] for w in words),max(w["bottom"] for w in words)]
            assert len(text)>20 and "\ufffd" not in text
            assert bounds[0]>20 and bounds[1]>20 and bounds[2]<page.width-20 and bounds[3]<page.height-20
            with Image.open(image) as raster:
                assert raster.width>800 and raster.height>1100
            pages.append(dict(page=page.page_number,text=text,content_bounds=bounds,
                              size=[page.width,page.height],raster=image.name))
    assert len(pages)>=2
    whole="\n".join(p["text"] for p in pages)
    markers=("贵阳旅行路书","行程版本 r9","模拟数据","室内展馆","待核实","来源与证据","采集","适用","全团")
    for word in markers:
        assert word in whole,word
    result=dict(status="PASS",page_count=len(pages),matched_text=list(markers),pages=pages)
    (output/"text-and-pages.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return {"status":"PASS","pages":len(pages),"matched":list(markers)}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--poppler",type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.pdf,args.output,args.poppler),ensure_ascii=False))
