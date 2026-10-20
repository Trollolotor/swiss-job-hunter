export function vacancyQuery({profileId,direction,status="all",q="",minStars=0,sort="priority",publishedWithin="",directOnly=false,reviewQueue=false,minScore=0}) {
  const params=new URLSearchParams({status,q,direction:direction||"all",min_stars:String(minStars),sort,
    published_within:String(publishedWithin),direct_only:String(directOnly),review_queue:String(reviewQueue),min_score:String(minScore)});
  if(profileId)params.set("profile_id",String(profileId));
  return params;
}
