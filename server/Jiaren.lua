-- tlbb-bot: version-bound integration source. Save in the target server encoding before installation.
-- Every entry requires an administrator-maintained GUID allowlist.
x9998496_g_scriptId = 9998496
x9998496_huntMapDataId = {
   [3]=32543, [4]=32543, [5]=32543, [8]=32543,
   [18]=32545, [19]=32546, [20]=32548, [21]=32549,
   [22]=32551, [23]=32552, [24]=32544, [25]=32546,
   [26]=32547, [27]=32549, [28]=32550, [29]=32551,
   [30]=32544, [31]=32545, [32]=32547, [33]=32548,
   [34]=32550, [35]=32551,
}



--********************************************************************
--回调函数
--********************************************************************
function x9998496_Jiaren(sceneId, selfId, TYPE, var1, var2, var3, var4, var5)
   if x9998496_IsManagedSpeaker(sceneId,selfId) ~= 1 then return end


   if TYPE == 1 then  --传送

			NewWorld( sceneId, selfId, var1, var2, var3)

		return
   end

   if TYPE == 2 then  --满怒治疗
			RestoreHp( sceneId, selfId ) ------满血
			RestoreMp( sceneId, selfId ) ------满气
			RestoreRage( sceneId, selfId ) ------满怒
		return
   end

   if TYPE == 14 then
      x9998496_BanditSupply(sceneId,selfId,var1,var2,var3,var4)
      return
   end

   if TYPE == 13 then  -- actual membership fields for CGChat channel routing
      if var2 ~= sceneId or var3 ~= selfId then return end
      if type(GetTeamId) ~= "function" or type(GetHumanGuildID) ~= "function" or type(GetMenPai) ~= "function" then
         return
      end
      local team, guild, sect = GetTeamId(sceneId,selfId), GetHumanGuildID(sceneId,selfId), GetMenPai(sceneId,selfId)
      if type(team) ~= "number" or type(guild) ~= "number" or type(sect) ~= "number" then return end
      Msg2Player(sceneId,selfId,format("JCH1:%d:%d:%d:%.0f:%.0f:%.0f",selfId,sceneId,var1,team,guild,sect),MSG2PLAYER_PARA)
      return
   end

   if TYPE == 8 then  -- bounded, read-only state of one treasure bandit
      if var3 ~= sceneId then return end
      local banditDataId = x9998496_huntMapDataId[sceneId]
      local health, x, z = "MISSING", 0, 0
      if banditDataId ~= nil and var1 > 0 and LuaFnIsObjValid(sceneId,var1) == 1 then
         if GetMonsterDataID(sceneId,var1) == banditDataId then
            health = "UNKNOWN"
            if type(GetHp) == "function" then
               local value = GetHp(sceneId,var1)
               if type(value) == "number" and value >= 0 then
                  health = format("%d",value)
                  x,z = LuaFnGetWorldPos(sceneId,var1)
               end
            end
         end
      end
      if x == nil or z == nil then health, x, z = "UNKNOWN", 0, 0 end
      Msg2Player(sceneId,selfId,format("JHT1:%d:%d:%d:%d:%s:%.3f:%.3f",
         selfId,sceneId,var2,var1,health,x,z),MSG2PLAYER_PARA)
      return
   end

   if TYPE == 7 then  -- read-only readiness of the normal followed team
      if var2 ~= sceneId or var3 ~= selfId then return end
      local following = IsTeamFollow(sceneId,selfId)
      local count = 0
      if following == 1 then count = LuaFnGetFollowedMembersCount(sceneId,selfId) end
      if type(count) ~= "number" or count < 0 or count > 6 then return end
      local missing, guids = 0, ""
      for i = 0, count - 1 do
         local member = GetFollowedMember(sceneId,selfId,i)
         if member ~= nil and member >= 0 and LuaFnIsObjValid(sceneId,member) == 1 then
            local guid = GetHumanGUID(sceneId,member)
            if guid ~= nil and guid > 0 then
               if guids ~= "" then guids = guids.."," end
               guids = guids..format("%d",guid)
            else missing = missing + 1 end
         else missing = missing + 1 end
      end
      Msg2Player(sceneId,selfId,format("JRF1:%d:%d:%d:%d:%d:%d:%d:%s",
         selfId,sceneId,var1,following,count,missing,LuaFnGetLevel(sceneId,selfId),guids),
         MSG2PLAYER_PARA)
      return
   end

   if TYPE == 6 then  -- read-only position feedback for continuous hunt movement
      if var2 ~= sceneId or var3 ~= selfId then return end
      local x, z = LuaFnGetWorldPos(sceneId,selfId)
      if x == nil or z == nil then return end
      Msg2Player(sceneId,selfId,format("JRP1:%d:%d:%d:%.3f:%.3f",
         selfId,sceneId,var1,x,z),MSG2PLAYER_PARA)
      return
   end

   if TYPE == 5 then  -- read actual mount state before a return-city skill
      if type(LuaFnIsRiding) ~= "function" then
         Msg2Player(sceneId,selfId,"JRS1:ERROR:RIDING_API_UNAVAILABLE",MSG2PLAYER_PARA)
         return
      end
      local riding = LuaFnIsRiding(sceneId,selfId)
      Msg2Player(sceneId,selfId,"JRS1:"..tostring(selfId)..":"..tostring(sceneId)..":"
         ..tostring(riding),MSG2PLAYER_PARA)
      return
   end

   if TYPE == 4 then  -- managed-role durability audit / maintenance
      if var1 ~= 0 and var1 ~= 1 then return end
      local allowed = dofile("/home/tlbb1/Public/Data/Script/JiarenRepairManaged.lua")
      local guid = GetHumanGUID(sceneId,selfId)
      if type(allowed) ~= "table" or type(allowed.guids) ~= "table"
         or type(allowed.items) ~= "table" or allowed.guids[guid] ~= true then
         Msg2Player(sceneId,selfId,"JRR1:ERROR:NOT_MANAGED",MSG2PLAYER_PARA)
         return
      end
      if x9998496_repairDisabled == nil then x9998496_repairDisabled = {} end
      if var1 == 1 and x9998496_repairDisabled[guid] == true then
         Msg2Player(sceneId,selfId,"JRR1:ERROR:RESTORE_DISABLED",MSG2PLAYER_PARA)
         return
      end
      local now = LuaFnGetCurrentTime()
      if type(now) ~= "number" then return end
      if x9998496_repairLast == nil then x9998496_repairLast = {} end
      if x9998496_repairLast[guid] ~= nil and now-x9998496_repairLast[guid] < 10 then return end
      x9998496_repairLast[guid] = now
      local slots = {240,241,242,243,244,245,246,247,251,252,253,254,255}
      local report = ""
      local changed = 0
      local failed = 0
      for _,slot in slots do
         local itemId = LuaFnGetItemTableIndexByIndex(sceneId,selfId,slot)
         local def = allowed.items[itemId]
         if def ~= nil and LuaFnGetBagEquipType(sceneId,selfId,slot) == def.point then
            local current, nativeMax = GetBagItemDur(sceneId,selfId,slot)
            local maximum = def.max
            if type(nativeMax) == "number" and nativeMax > 0 then maximum = nativeMax end
            local after = current
            local result = "AUDIT"
            if var1 == 1 then
               if type(current) ~= "number" or type(maximum) ~= "number"
                  or maximum <= 0 or maximum > 100000 or current < 0 or current > maximum
                  or floor(current) ~= current or floor(maximum) ~= maximum then
                  failed = failed+1
                  result = "INVALID_DURABILITY"
               elseif current < maximum then
                  SetBagItemDur(sceneId,selfId,slot,maximum)
                  after = GetBagItemDur(sceneId,selfId,slot)
                  if after == maximum then
                     changed = changed+1
                     result = "RESTORED"
                  else
                     failed = failed+1
                     result = "VERIFY_FAILED"
                     x9998496_repairDisabled[guid] = true
                  end
               else
                  result = "FULL"
               end
            end
            if report ~= "" then report = report.."|" end
            report = report..tostring(slot)..","..tostring(itemId)..","..tostring(current)
               ..","..tostring(nativeMax)..","..tostring(def.max)..","..tostring(after)..","..result
            if result == "VERIFY_FAILED" then break end
         end
      end
      Msg2Player(sceneId,selfId,"JRR1:"..tostring(guid)..":"..tostring(var1)..":"
         ..tostring(changed)..":"..tostring(failed)..":"..report,MSG2PLAYER_PARA)
      return
   end

   if TYPE == 3 then  -- bounded read-only treasure bandit lookup
      local banditDataId = x9998496_huntMapDataId[sceneId]
      if banditDataId == nil then
         return
      end
      if type(GetHp) ~= "function" then return end
      local count = GetMonsterCount(sceneId)
      local selfX, selfZ = LuaFnGetWorldPos(sceneId, selfId)
      local bestId, bestX, bestZ, bestDistance = 0, 0, 0, 1000000000
      for i = 0, count - 1 do
         local monsterId = GetMonsterObjID(sceneId, i)
         if monsterId ~= nil and monsterId >= 0 and LuaFnIsObjValid(sceneId, monsterId) == 1 then
            if GetMonsterDataID(sceneId, monsterId) == banditDataId
               and monsterId ~= var1 and monsterId ~= var2 and monsterId ~= var3
               then
               local hp = GetHp(sceneId,monsterId)
               if type(hp) == "number" and hp > 0 then
               local x, z = LuaFnGetWorldPos(sceneId, monsterId)
               if x ~= nil and z ~= nil then
                  local dx, dz = x - selfX, z - selfZ
                  local distance = dx * dx + dz * dz
                  if distance < bestDistance then
                     bestId, bestX, bestZ, bestDistance = monsterId, x, z, distance
                  end
               end
               end
            end
         end
      end
      if bestId == 0 then
         local unused1, unused2, x, z, unused3 = CallScriptFunction(229020, "GetScenePosInfo", sceneId, sceneId)
         if x ~= nil and z ~= nil then
            bestX, bestZ = x, z
         end
      end
      Msg2Player(sceneId, selfId, format("JRH1:%d:%d:%.2f:%.2f", sceneId, bestId, bestX, bestZ), MSG2PLAYER_PARA)
      return
   end

   if TYPE == 50 then  --自定义函数

		return
   end
   if TYPE == 51 then  --自定义函数

		return
   end

end


-- Allow only GUIDs provisioned by the managed-account control center.
function x9998496_IsManagedSpeaker(sceneId,selfId)
   if type(openfile) ~= "function" or type(read) ~= "function" or type(closefile) ~= "function" then return 0 end
   local guid = GetHumanGUID(sceneId,selfId)
   local file = openfile("/home/tlbb1/Public/Data/Script/bot_speaker_allowlist.txt","r")
   if file == nil then return 0 end
   local allowed = 0
   for i = 1, 4096 do
      local line = read(file,"*l")
      if line == nil then break end
      if strfind(line,"^%d+$") and tonumber(line) == guid then allowed = 1 break end
   end
   closefile(file)
   return allowed
end

-- Count all living bandits, including temporarily rejected combat targets.
function x9998496_CountBandits(sceneId)
   local dataId = x9998496_huntMapDataId[sceneId]
   if dataId == nil or type(GetHp) ~= "function" then return nil end
   local total = GetMonsterCount(sceneId)
   if type(total) ~= "number" or total < 0 or total > 100000 then return nil end
   local alive = 0
   for i = 0, total - 1 do
      local obj = GetMonsterObjID(sceneId,i)
      if obj ~= nil and obj >= 0 and LuaFnIsObjValid(sceneId,obj) == 1
         and GetMonsterDataID(sceneId,obj) == dataId then
         local hp = GetHp(sceneId,obj)
         if type(hp) ~= "number" then return nil end
         if hp > 0 then alive = alive + 1 end
      end
   end
   return alive
end

function x9998496_BanditSupply(sceneId,selfId,sequence,expectedScene,expectedObj,request)
   if expectedScene ~= sceneId or expectedObj ~= selfId then return end
   if request ~= 0 and request ~= 1 then return end
   if type(sequence) ~= "number" or sequence < 0 or sequence > 2147483647 then return end
   if x9998496_huntMapDataId[sceneId] == nil then return end
   if x9998496_IsManagedSpeaker(sceneId,selfId) ~= 1
      or type(LuaFnIsTeamLeader) ~= "function" or LuaFnIsTeamLeader(sceneId,selfId) ~= 1 then
      Msg2Player(sceneId,selfId,format("JBS1:%d:%d:%d:0:DENIED",selfId,sceneId,sequence),MSG2PLAYER_PARA)
      return
   end
   local count = x9998496_CountBandits(sceneId)
   if count == nil then return end
   local status = "OBSERVED"
   if request == 1 then
      if count > 0 then status = "ALIVE"
      else
         -- Local controller also serializes all teams with a persistent,
         -- server-scoped 30-minute reservation. This is a second map guard.
         if type(time) ~= "function" then return end
         local now = time()
         if type(now) ~= "number" then return end
         if x9998496_banditSupplyLast == nil then x9998496_banditSupplyLast = {} end
         local last = x9998496_banditSupplyLast[sceneId]
         if last ~= nil and now - last < 1800 then status = "COOLDOWN"
         else
            x9998496_banditSupplyLast[sceneId] = now
            CallScriptFunction(229019,"CreateMonster",sceneId)
            count = x9998496_CountBandits(sceneId)
            if count == nil then return end
            if count > 0 then status = "SPAWNED" else status = "FAILED" end
         end
      end
   end
   Msg2Player(sceneId,selfId,format("JBS1:%d:%d:%d:%d:%s",selfId,sceneId,sequence,count,status),MSG2PLAYER_PARA)
end
